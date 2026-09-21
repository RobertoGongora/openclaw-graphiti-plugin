"""Read-only graph snapshots rendered to PNG by the Graphviz system executable."""

import base64
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import textwrap
from collections import Counter
from datetime import UTC, datetime

from neo4j import WRITE_ACCESS
from neo4j.exceptions import Neo4jError
from neo4j.graph import Node, Path, Relationship

COLORS = {
    "MemoryEntity": "#ffc5ca",
    "MemoryFact": "#c9cd98",
    "MemoryEpisode": "#ffd36d",
    "MemoryDream": "#ffb879",
    "MemoryInsight": "#f3a6cf",
    "MemorySession": "#90caf9",
    "MemoryMessage": "#b0bec5",
    "MemoryArtifact": "#80cbc4",
    "MemoryArtifactObservation": "#ce93d8",
}
# Tokenize quoted strings/identifiers and comments before checking query features.
# EXPLAIN is the authority on whether the statement writes. Procedure calls and
# namespaced functions are additionally blocked, even when marked read-only.
LEX = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`|[A-Za-z_][A-Za-z_0-9]*|[^\s]"
)
MAX_IMAGE = 4_000_000
LAYOUT_TIMEOUT = 45
FORBIDDEN = set(
    "CALL LOAD CYPHER EXPLAIN PROFILE SHOW USE CREATE MERGE DELETE DETACH SET REMOVE DROP ALTER GRANT DENY REVOKE FOREACH TERMINATE START STOP".split()
)


def read_query(cypher):
    query = cypher.strip().removesuffix(";").strip()
    tokens = []
    for token in LEX.findall(query):
        if token.startswith(("//", "/*")):
            continue
        if token.startswith(("'", '"', "`")):
            token = "quoted_identifier" if token.startswith("`") else "literal"
        tokens.append(token.upper())
    if not tokens or tokens[0] not in {"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN"}:
        raise ValueError(
            "Use a read-only MATCH/RETURN query returning nodes, relationships, or paths."
        )
    if ";" in tokens or FORBIDDEN.intersection(tokens):
        raise ValueError(
            "Rendering permits one read-only query; writes, procedures, and imports are disabled."
        )
    if re.search(r"\b\w+\s*\.\s*\w+\s*\(", " ".join(tokens)):
        raise ValueError("Namespaced functions are disabled for graph rendering.")
    return query


class Snapshot:
    def __init__(self, request):
        self.request = request
        self.nodes, self.edges = {}, {}
        self.pending_edges = {}
        self.truncated = set()
        self.values_seen = 0

    def allowed(self, node):
        return node.get("namespace") == self.request.namespace and any(
            k in node.labels for k in COLORS
        )

    def node(self, node):
        if node.element_id in self.nodes or not self.allowed(node):
            return
        self.record_node(node.element_id, node.labels, node)

    def record_node(self, identity, labels, props):
        if identity in self.nodes:
            return
        if len(self.nodes) >= self.request.max_nodes:
            self.truncated.add("nodes")
            return
        label = next(k for k in COLORS if k in labels)
        # Explicit captions only: never render payloads, retry candidates, or credentials.
        caption = props.get("name") or props.get("summary")
        if label == "MemoryEpisode" and not caption:
            caption = str(props.get("source_id") or "Episode").rsplit("/", 1)[-1]
        self.nodes[identity] = {"kind": label, "caption": str(caption or label[6:])[:2000]}

    def edge(self, edge):
        if edge.element_id in self.edges:
            return
        if any(n.get("namespace") is None and n.element_id not in self.nodes for n in edge.nodes):
            if len(self.pending_edges) <= self.request.max_relationships:
                self.pending_edges[edge.element_id] = edge
            else:
                self.truncated.add("relationships")
            return
        if not all(n.element_id in self.nodes or self.allowed(n) for n in edge.nodes):
            return
        for node in edge.nodes:
            self.node(node)
        if not all(n.element_id in self.nodes for n in edge.nodes):
            return
        if len(self.edges) >= self.request.max_relationships:
            self.truncated.add("relationships")
            return
        self.edges[edge.element_id] = (
            edge.start_node.element_id,
            edge.end_node.element_id,
            edge.type,
        )

    def add(self, value, depth=0):
        self.values_seen += 1
        if self.values_seen > 500_000 or depth > 20:
            self.truncated.add("result_values")
            return
        if isinstance(value, Node):
            self.node(value)
        elif isinstance(value, Relationship):
            self.edge(value)
        elif isinstance(value, Path):
            for node in value.nodes:
                self.add(node, depth + 1)
            for edge in value.relationships:
                self.add(edge, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                self.add(child, depth + 1)
                if "result_values" in self.truncated:
                    break
        elif isinstance(value, (list, tuple)):
            for child in value:
                self.add(child, depth + 1)
                if "result_values" in self.truncated:
                    break


def snapshot(store, request):
    graph = Snapshot(request)
    params = {**request.parameters, "namespace": request.namespace, "ns": request.namespace}
    # Route to the writer for fresh state, as recall does. READ_ACCESS alone is
    # a routing hint, not a write prohibition. EXPLAIN + feature restrictions
    # enforce read-only statements; the explicit transaction is always rolled back.
    with store.driver.session(
        database=store.database, default_access_mode=WRITE_ACCESS, fetch_size=500
    ) as session:
        tx = session.begin_transaction(timeout=15)
        try:
            if request.cypher is None:
                from .journal import LABELS

                # One labelled branch each, so label/namespace indexes apply; an
                # unlabelled namespace match scans every node in the database.
                branches = " UNION ALL ".join(
                    f"MATCH (n:{label} {{namespace:$namespace}}) RETURN n ORDER BY n.id LIMIT $cap"
                    for label in LABELS
                    if label in COLORS
                )
                for row in tx.run(
                    f"CALL {{ {branches} }} "
                    "RETURN elementId(n) AS id, labels(n) AS labels, n.name AS name, "
                    "n.summary AS summary, n.source_id AS source_id ORDER BY n.id LIMIT $cap",
                    namespace=request.namespace,
                    cap=request.max_nodes + 1,
                ):
                    graph.record_node(row["id"], row["labels"], row.data())
                for row in tx.run(
                    "MATCH (n {namespace:$namespace})-[r]->(m {namespace:$namespace}) "
                    "WHERE elementId(n) IN $ids AND elementId(m) IN $ids "
                    "RETURN elementId(r) AS id,elementId(n) AS source,elementId(m) AS target,type(r) AS kind LIMIT $cap",
                    namespace=request.namespace,
                    ids=list(graph.nodes),
                    cap=request.max_relationships + 1,
                ):
                    if len(graph.edges) >= request.max_relationships:
                        graph.truncated.add("relationships")
                        break
                    graph.edges[row["id"]] = (row["source"], row["target"], row["kind"])
            else:
                query = read_query(request.cypher)
                summary = tx.run("EXPLAIN " + query, params).consume()
                if summary.query_type != "r":
                    raise ValueError("Rendering requires a read-only Cypher query.")
                # Stream only a bounded number of rows. Do not wrap the query:
                # Cypher subqueries reject otherwise-valid unaliased expressions.
                # Transaction timeout also covers discarding the unread remainder.
                cap = request.max_nodes + request.max_relationships
                result = tx.run(query, params)
                for index, row in enumerate(result):
                    if index >= cap:
                        graph.truncated.add("rows")
                        break
                    for value in row.values():
                        graph.add(value)
                    if "result_values" in graph.truncated:
                        break
                result.consume()  # DISCARD, not buffer, unrendered rows before another query.
                # RETURN r alone contains endpoint identities but no properties.
                # Hydrate them within the requested namespace before drawing.
                if graph.pending_edges:
                    ids = list(
                        {n.element_id for r in graph.pending_edges.values() for n in r.nodes}
                    )
                    for row in tx.run(
                        "MATCH (n {namespace:$namespace}) WHERE elementId(n) IN $ids RETURN n",
                        namespace=request.namespace,
                        ids=ids,
                    ):
                        graph.add(row["n"])
                    for edge in list(graph.pending_edges.values()):
                        if all(n.element_id in graph.nodes for n in edge.nodes):
                            graph.edge(edge)
        except Neo4jError as exc:
            raise ValueError(
                "Cypher could not be rendered. Check syntax/parameters and simplify queries that exceed the 15-second limit."
            ) from exc
        finally:
            tx.rollback()
    return graph


def quoted(text):
    # JSON quoting is valid DOT quoting; doubling backslashes prevents Graphviz
    # escape substitutions. All labels are plain quoted text, never HTML.
    return json.dumps(text, ensure_ascii=False)


def dot_source(graph):
    nodes, edges = graph.nodes, graph.edges
    detailed = len(nodes) <= 100
    degree = Counter(n for a, b, _ in edges.values() for n in (a, b))
    hubs = set(
        sorted(
            (n for n in nodes if nodes[n]["kind"] == "MemoryEntity"),
            key=lambda n: (-degree[n], nodes[n]["caption"]),
        )[:18]
    )
    counts = Counter(n["kind"][6:] for n in nodes.values())
    legend = "   |   ".join(f"{k}: {v:,}" for k, v in sorted(counts.items()))
    title = f"{graph.request.namespace}  ·  {len(nodes):,} nodes  ·  {len(edges):,} relationships"
    if graph.truncated:
        title += "  ·  PARTIAL VIEW (limit reached)"
    caption = title + "\n" + legend
    if not detailed:
        caption += "\nOverview: labels on major entity hubs. Use a focused query for fact captions."
    lines = [
        "digraph memory {",
        'graph [bgcolor="#191c1e", fontcolor="#d9dfe3", fontname="DejaVu Sans", fontsize=16, pad=0.3, overlap=prism, splines=line, outputorder=edgesfirst, start=42, size="20,14!", dpi=160, labelloc=t, label='
        + quoted(caption)
        + "];",
        'node [shape=circle, style=filled, penwidth=0, fontname="DejaVu Sans", fontsize=10, fontcolor="#202326", margin=0.06];',
        'edge [color="#69758070", penwidth=0.5, arrowsize=0.35, fontname="DejaVu Sans", fontsize=8, fontcolor="#a2aeb8"];',
    ]
    if not nodes:
        lines.append(
            'empty [shape=plaintext, fontcolor="#d9dfe3", label="No graph nodes matched this view"];'
        )
    for key, node in nodes.items():
        if detailed:
            label = "\n".join(
                textwrap.wrap(" ".join(node["caption"].split()), 24, max_lines=4, placeholder="…")
            )
            attrs = f"label={quoted(label)}, fillcolor={quoted(COLORS[node['kind']])}"
        else:
            label = (
                textwrap.shorten(node["caption"], width=28, placeholder="…") if key in hubs else ""
            )
            size = min(0.45, 0.055 + 0.025 * math.sqrt(degree[key]))
            attrs = f'label="", xlabel={quoted(label)}, fontcolor="#e7edf2", fontsize=12, width={size:.3f}, height={size:.3f}, fixedsize=true, fillcolor={quoted(COLORS[node["kind"]])}'
        lines.append(f"{quoted(key)} [{attrs}];")
    for a, b, relation in edges.values():
        label = f"label={quoted(relation)}" if detailed else ""
        lines.append(f"{quoted(a)} -> {quoted(b)} [{label}];")
    lines.append("}")
    return "\n".join(lines)


def render_graph(store, request):
    executable = shutil.which("sfdp")
    if executable is None:
        raise ValueError(
            "Graph rendering requires Graphviz (sfdp). It is included in the Docker image."
        )
    graph = snapshot(store, request)
    # Own process group: a timed-out layout is killed with anything it spawned.
    process = subprocess.Popen(
        [executable, "-Tpng"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        image, _ = process.communicate(dot_source(graph).encode(), timeout=LAYOUT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise ValueError(
            "Graph layout exceeded 45 seconds; use a focused Cypher query or lower limits."
        ) from exc
    if process.returncode or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(
            "Graphviz could not render this view; simplify the query or lower the limits."
        )
    width, height = struct.unpack(">II", image[16:24])
    limits = set(graph.truncated)
    if len(image) > MAX_IMAGE:
        limits.add("image_bytes")
    result = {
        "namespace": request.namespace,
        "rendered_at": datetime.now(UTC).isoformat(),
        "nodes": len(graph.nodes),
        "relationships": len(graph.edges),
        "node_types": dict(Counter(n["kind"] for n in graph.nodes.values())),
        "relationship_types": dict(Counter(r for _, _, r in graph.edges.values())),
        "truncated": bool(limits),
        "limits_reached": sorted(limits),
        "max_nodes": request.max_nodes,
        "max_relationships": request.max_relationships,
        "labels": "all" if len(graph.nodes) <= 100 else "major_entity_hubs",
        "width": width,
        "height": height,
    }
    # An oversized picture is reported, not inlined as a multi-megabyte base64 body.
    if len(image) <= MAX_IMAGE:
        result["image"] = {"mimeType": "image/png", "data": base64.b64encode(image).decode()}
    return result
