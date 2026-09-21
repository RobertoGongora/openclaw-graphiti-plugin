import base64
import shutil

import pytest

from graph_memory.journal import Journal
from graph_memory.mcp import Protocol
from graph_memory.models import Render, Transcript
from graph_memory.render import dot_source, read_query, snapshot
from graph_memory.service import MemoryService
from tests.helpers import MYSQL, PROJECT, ingest
from tests.test_contracts import headers, rpc


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) SET n.name='changed' RETURN n",
        "MATCH (n) RETURN n; MATCH (m) DELETE m",
        "CALL db.labels()",
        "MATCH (n) CALL { CREATE (x) } RETURN n",
        "LOAD CSV FROM 'https://example.com' AS row RETURN row",
        "RETURN apoc.load.json('https://example.com')",
        "RETURN `apoc`.`load.json`('file:///secret')",
        "CYPHER runtime=slotted MATCH (n) RETURN n",
    ],
)
def test_render_query_rejects_mutations_and_external_procedures(query):
    with pytest.raises(ValueError):
        read_query(query)


def test_render_query_allows_comments_and_quoted_words():
    query = "/* CREATE isn't executed */ MATCH (n) WHERE n.name = 'DELETE; CALL' RETURN n;"
    assert read_query(query) == query[:-1]


def test_default_snapshot_contains_orphans_and_custom_paths_stay_scoped(graph):
    store, ns = graph
    _, source, _ = ingest(
        store,
        ns,
        "render",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [{"subject": PROJECT["key"], "target": MYSQL["key"], "relation": "uses_database"}],
    )
    store.stage(Transcript.model_validate({**source.model_dump(), "source_id": "orphan"}))
    before = Journal(store).verify(ns)
    request = Render(namespace=ns)
    assert request.max_nodes == 300 and request.max_relationships == 1_000
    full = snapshot(store, request)
    assert len(full.nodes) == 5 and len(full.edges) == 3
    assert not full.truncated
    assert "Atlas uses MySQL." in dot_source(full)
    assert "HAS_FACT" in dot_source(full)
    assert len(snapshot(store, Render(namespace=ns, cypher="MATCH (n) RETURN n")).nodes) == 5
    path = snapshot(
        store,
        Render(namespace=ns, cypher="MATCH p=(n {namespace:$namespace})-[:HAS_FACT]->() RETURN p"),
    )
    assert len(path.nodes) == 2 and len(path.edges) == 1
    edges = snapshot(store, Render(namespace=ns, cypher="MATCH ()-[r:HAS_FACT]->() RETURN r"))
    assert len(edges.nodes) == 2 and len(edges.edges) == 1
    nested = snapshot(
        store,
        Render(namespace=ns, cypher="MATCH (n {namespace:$namespace}) RETURN {nodes:collect(n)}"),
    )
    assert len(nested.nodes) == 5
    assert not snapshot(store, Render(namespace=ns, cypher="RETURN 42")).nodes
    limited = snapshot(store, Render(namespace=ns, max_relationships=1))
    assert len(limited.edges) == 1 and "relationships" in limited.truncated
    limited = snapshot(store, Render(namespace=ns, max_nodes=2))
    assert len(limited.nodes) == 2 and "nodes" in limited.truncated
    # Another namespace cannot be rendered, even if the query finds it.
    foreign_id = ns + "-foreign"
    store.transaction(
        lambda tx: tx.run(
            "CREATE (:MemoryEntity {id:$id,namespace:'private-other',name:'hidden'})", id=foreign_id
        ).consume()
    )
    try:
        visible = snapshot(store, Render(namespace=ns, cypher="MATCH (n) RETURN n"))
        assert len(visible.nodes) == 5
        assert "hidden" not in dot_source(visible)
        for query in ("MATCH (n) DELETE n", "MATCH (n) SET n.name='oops' RETURN n"):
            with pytest.raises(ValueError):
                snapshot(store, Render(namespace=ns, cypher=query))
    finally:
        store.transaction(lambda tx: tx.run("MATCH (n {id:$id}) DELETE n", id=foreign_id).consume())
    assert Journal(store).verify(ns) == before


@pytest.mark.skipif(not shutil.which("sfdp"), reason="Graphviz system package required")
def test_mcp_render_returns_png_content_and_metadata_without_mutating_graph(graph):
    store, ns = graph
    ingest(
        store,
        ns,
        "render",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [{"subject": PROJECT["key"], "target": MYSQL["key"], "relation": "uses_database"}],
    )
    before = Journal(store).verify(ns)
    server = Protocol(MemoryService(store), namespace=ns, read_only=True)
    message = rpc("tools/call", name="memory_render", arguments={"namespace": ns})
    status, response = server.dispatch(message, headers(message))
    assert status == 200 and not response["result"]["isError"]
    result = response["result"]
    picture = next(c for c in result["content"] if c["type"] == "image")
    assert picture["mimeType"] == "image/png"
    assert base64.b64decode(picture["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert result["structuredContent"]["nodes"] == 4
    assert "image" not in result["structuredContent"]  # No duplicate base64 payload.
    assert 0 < result["structuredContent"]["width"] <= 3300
    assert 0 < result["structuredContent"]["height"] <= 2350
    assert Journal(store).verify(ns) == before
    bad = rpc("tools/call", name="memory_render", arguments={"namespace": "other"})
    assert server.dispatch(bad, headers(bad))[0] == 403


def test_missing_graphviz_is_actionable(monkeypatch):
    from graph_memory.render import render_graph

    monkeypatch.setattr("graph_memory.render.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="Graphviz"):
        render_graph(None, Render(namespace="personal"))


def fake_sfdp(tmp_path, monkeypatch, script):
    from graph_memory import render

    executable = tmp_path / "sfdp"
    executable.write_text("#!/bin/sh\n" + script)
    executable.chmod(0o755)
    monkeypatch.setattr(render.shutil, "which", lambda _: str(executable))
    monkeypatch.setattr(render, "snapshot", lambda _, request: render.Snapshot(request))
    return render


PNG_HEAD = r"printf '\211PNG\r\n\032\n\000\000\000\rIHDR\000\000\000\001\000\000\000\002'"


def test_oversized_image_is_flagged_instead_of_inlined(tmp_path, monkeypatch):
    render = fake_sfdp(tmp_path, monkeypatch, f"cat >/dev/null\n{PNG_HEAD}\nhead -c 64 /dev/zero\n")
    small = render.render_graph(None, Render(namespace="personal"))
    assert small["image"]["mimeType"] == "image/png" and not small["truncated"]
    assert (small["width"], small["height"]) == (1, 2)
    monkeypatch.setattr(render, "MAX_IMAGE", 32)
    large = render.render_graph(None, Render(namespace="personal"))
    assert "image" not in large
    assert large["truncated"] and large["limits_reached"] == ["image_bytes"]


def test_layout_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    import os
    import time

    marker = tmp_path / "child.pid"
    render = fake_sfdp(tmp_path, monkeypatch, f"sleep 60 &\necho $! > {marker}\nwait\n")
    monkeypatch.setattr(render, "LAYOUT_TIMEOUT", 1)
    with pytest.raises(ValueError, match="layout exceeded"):
        render.render_graph(None, Render(namespace="personal"))
    child = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("sfdp descendant survived the timeout")


def test_default_snapshot_queries_each_label_so_indexes_apply(graph):
    store, ns = graph
    queries = []

    class Recording:
        def __init__(self, tx):
            self.tx = tx

        def run(self, query, *args, **kwargs):
            queries.append(query)
            return self.tx.run(query, *args, **kwargs)

        def rollback(self):
            return self.tx.rollback()

    class Sessions:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            self.inner = self.session.__enter__()
            return self

        def __exit__(self, *exc):
            return self.session.__exit__(*exc)

        def begin_transaction(self, **kwargs):
            return Recording(self.inner.begin_transaction(**kwargs))

    class Driver:
        def session(self, **kwargs):
            return Sessions(store.driver.session(**kwargs))

    class Store:
        driver, database = Driver(), store.database

    assert not snapshot(Store, Render(namespace=ns)).nodes
    from graph_memory.render import COLORS

    for label in COLORS:
        assert f"(n:{label} {{namespace:$namespace}})" in queries[0]
    assert "MATCH (n {namespace" not in queries[0]
