"""Snapshot everything countable about a live graph, so later graphs can be compared.

Read-only. Counts, distributions and crosstabs only: no message content, no fact
text. Names (entities, subjects, tools that name paths, session URIs) appear only
with --names, for a report that stays out of the repository.

    NEO4J_URI=bolt://127.0.0.1:27687 NEO4J_PASSWORD=... uv run python -m evals.graph_baseline \\
        --output evals/reports/20260922-graph-baseline.json \\
        --container-prefix graph-memory-transcripts

The container prefix adds what the graph does not hold: images, memory, store
size, and the worker's processing log since its container started.
"""

import argparse
import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from neo4j import GraphDatabase

from graph_memory.version import engine_fingerprint

SECRET = ("PASSWORD", "TOKEN", "SECRET", "AUTH", "KEY", "CREDENTIAL")


def dist(expr, prefix=""):
    """RETURN fragment: the usual summary of a numeric expression."""
    p = prefix and prefix + "_"
    return (
        f"count({expr}) AS {p}count, min({expr}) AS {p}min, "
        f"percentileDisc({expr},0.5) AS {p}p50, percentileDisc({expr},0.9) AS {p}p90, "
        f"percentileDisc({expr},0.99) AS {p}p99, max({expr}) AS {p}max, "
        f"round(avg({expr}),2) AS {p}avg, sum({expr}) AS {p}sum"
    )


def jsonable(value) -> dict | list | int | float | str | bool | None:
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)


class Reader:
    def __init__(self, driver, database, namespace):
        self.driver, self.database, self.ns = driver, database, namespace
        self.timings = {}

    def rows(self, name, query, **params) -> list[dict]:
        started = time.monotonic()
        with self.driver.session(database=self.database, default_access_mode="READ") as s:
            data = s.run(query, ns=self.ns, **params).data()
        self.timings[name] = round(time.monotonic() - started, 2)
        return [cast(dict, jsonable(row)) for row in data]

    def one(self, name, query, **params) -> dict:
        rows = self.rows(name, query, **params)
        return rows[0] if rows else {}

    def table(self, name, query, key="key", value="n", **params):
        """{key: value} from rows; None keys become 'null'."""
        return {
            "null" if r[key] is None else str(r[key]): r[value]
            for r in self.rows(name, query, **params)
        }


def schema(r):
    out = {
        "neo4j": r.rows(
            "components",
            "CALL dbms.components() YIELD name,versions,edition "
            "RETURN name,versions[0] AS version,edition",
        ),
        "labels": sorted(x["label"] for x in r.rows("labels", "CALL db.labels()")),
        "relationship_types": sorted(
            x["relationshipType"] for x in r.rows("rels", "CALL db.relationshipTypes()")
        ),
        "indexes": r.rows(
            "indexes",
            "SHOW INDEXES YIELD name,type,labelsOrTypes,properties,state "
            "RETURN name,type,labelsOrTypes,properties,state",
        ),
        "constraints": r.rows(
            "constraints",
            "SHOW CONSTRAINTS YIELD name,type,labelsOrTypes,"
            "properties RETURN name,type,labelsOrTypes,properties",
        ),
        "graph_counts": r.one(
            "graph_counts",
            "CALL db.stats.retrieve('GRAPH COUNTS') YIELD data "
            "RETURN data.nodes AS nodes,data.relationships AS relationships",
        ),
    }
    out["property_keys"] = {
        label: sorted(
            x["k"]
            for x in r.rows(
                f"keys:{label}",
                f"MATCH (n:`{label}`) WITH n LIMIT 1000 UNWIND keys(n) AS k RETURN DISTINCT k",
            )
        )
        for label in out["labels"]
    }
    return out


def nodes(r, labels, rel_types):
    out = {"by_label": {}, "degree": {}, "relationships": {}}
    for label in labels:
        out["by_label"][label] = r.table(
            f"count:{label}", f"MATCH (n:`{label}`) RETURN n.namespace AS key,count(*) AS n"
        )
        out["degree"][label] = r.one(
            f"degree:{label}",
            f"MATCH (n:`{label}`) WITH count{{(n)--()}} AS d "
            f"RETURN {dist('d')}, sum(CASE WHEN d=0 THEN 1 ELSE 0 END) AS isolated",
        )
    for t in rel_types:
        out["relationships"][t] = r.one(
            f"rel:{t}",
            f"MATCH (a)-[:`{t}`]->(b) RETURN count(*) AS n,"
            "count(DISTINCT a) AS distinct_sources,count(DISTINCT b) AS distinct_targets",
        )
    return out


def episodes(r):
    e = "MATCH (e:MemoryEpisode {namespace:$ns}) "
    return {
        "by_status": r.table("ep:status", e + "RETURN e.status AS key,count(*) AS n"),
        "by_engine": r.table("ep:engine", e + "RETURN e.engine AS key,count(*) AS n"),
        "by_model_info": r.table("ep:model", e + "RETURN e.model_info AS key,count(*) AS n"),
        "distinct_workers": r.one("ep:worker", e + "RETURN count(DISTINCT e.worker) AS n")["n"],
        "quarantined_by_status": r.table(
            "ep:quarantine",
            e + "WHERE e.quarantine_engine IS NOT NULL RETURN e.status AS key,count(*) AS n",
        ),
        "failed_by_error": r.table(
            "ep:errors",
            e + "WHERE e.status='failed' "
            "RETURN coalesce(e.last_error,e.error,'unknown') AS key,count(*) AS n",
        ),
        "quarantine_reasons": r.table(
            "ep:qreason",
            e + "WHERE e.quarantine_reason IS NOT NULL "
            "RETURN e.quarantine_reason AS key,count(*) AS n ORDER BY n DESC",
        ),
        "counters": r.one(
            "ep:counters",
            e + "RETURN sum(coalesce(e.attempts,0)) AS attempts,"
            "sum(coalesce(e.infra_failures,0)) AS infra_failures,"
            "sum(coalesce(e.validation_failures,0)) AS validation_failures,"
            "sum(CASE WHEN e.retry_feedback IS NOT NULL THEN 1 ELSE 0 END) AS with_retry_feedback,"
            "sum(CASE WHEN e.cached_extraction IS NOT NULL THEN 1 ELSE 0 END) AS with_cached_extraction,"
            "sum(CASE WHEN e.retry_after IS NOT NULL THEN 1 ELSE 0 END) AS with_retry_after,"
            "sum(CASE WHEN e.worker_lock IS NOT NULL THEN 1 ELSE 0 END) AS with_worker_lock",
        ),
        "attempts": r.one("ep:attempts", e + f"RETURN {dist('coalesce(e.attempts,0)')}"),
        "by_validation_engine": r.table(
            "ep:valengine", e + "RETURN e.validation_engine AS key,count(*) AS n"
        ),
        "fact_count_buckets": r.table(
            "ep:factbuckets",
            e + "WHERE e.status='complete' WITH coalesce(e.fact_count,0) AS c "
            "RETURN CASE WHEN c=0 THEN '0' WHEN c=1 THEN '1' WHEN c<=5 THEN '2-5' "
            "WHEN c<=10 THEN '6-10' WHEN c<=20 THEN '11-20' ELSE '21+' END AS key,count(*) AS n",
        ),
        "fact_count": r.one(
            "ep:factcount",
            e + f"WHERE e.status='complete' RETURN {dist('coalesce(e.fact_count,0)')}",
        ),
        "messages_per_episode": r.one("ep:msgs", e + f"RETURN {dist('size(e.message_refs)')}"),
        "payload_chars": r.one("ep:payload", e + f"RETURN {dist('size(e.payload)')}"),
        "extraction_payload_chars": r.one(
            "ep:extraction",
            e + "WHERE e.extraction_payload IS NOT NULL "
            f"RETURN {dist('size(e.extraction_payload)')}",
        ),
        "ingested_by_day": r.table(
            "ep:ingested",
            e + "RETURN substring(e.ingested_at,0,10) AS key,count(*) AS n ORDER BY key",
        ),
        "completed_by_day": r.table(
            "ep:completed",
            e + "WHERE e.completed_at IS NOT NULL "
            "RETURN substring(e.completed_at,0,10) AS key,count(*) AS n ORDER BY key",
        ),
        "completed_by_engine_and_day": r.rows(
            "ep:engineday",
            e + "WHERE e.completed_at IS NOT NULL "
            "RETURN e.engine AS engine,substring(e.completed_at,0,10) AS day,count(*) AS n "
            "ORDER BY day,engine",
        ),
        "per_session": r.one(
            "ep:persession", e + f"WITH e.session_ref AS s,count(*) AS c RETURN {dist('c')}"
        ),
        "has_fact_mismatch": r.one(
            "ep:mismatch",
            e + "WHERE e.status='complete' "
            "WITH e,count{(e)<-[:SUPPORTED_BY]-()} AS linked "
            "RETURN sum(CASE WHEN linked<>coalesce(e.fact_count,0) THEN 1 ELSE 0 END) AS n",
        )["n"],
        "with_cached_extraction_pending": r.one(
            "ep:cached",
            e + "WHERE e.status<>'complete' AND e.extraction_payload IS NOT NULL "
            "RETURN count(*) AS n",
        )["n"],
        "ingested_at_range": r.one(
            "ep:range",
            e + "RETURN min(e.ingested_at) AS first,max(e.ingested_at) AS last,"
            "max(e.completed_at) AS last_completed",
        ),
    }


def facts(r, names):
    f = "MATCH (f:MemoryFact {namespace:$ns}) "
    validated = "size(coalesce(f.validation_message_refs,[]))>0"
    out = {
        "by_status": r.table("f:status", f + "RETURN f.status AS key,count(*) AS n"),
        "retracted": r.one("f:retracted", f + "WHERE f.retracted RETURN count(*) AS n")["n"],
        "by_source_kind": r.table("f:kind", f + "RETURN f.source_kind AS key,count(*) AS n"),
        "by_time_basis": r.table("f:basis", f + "RETURN f.time_basis AS key,count(*) AS n"),
        "slots": r.one(
            "f:slot",
            f + "RETURN count(DISTINCT f.slot) AS distinct_slots,"
            "sum(CASE WHEN f.slot IS NOT NULL THEN 1 ELSE 0 END) AS facts_with_slot",
        ),
        "evidence_chars": r.one("f:evchars", f + f"RETURN {dist('size(f.evidence)')}"),
        "validation_evidence_chars": r.one(
            "f:valchars",
            f + "WHERE f.validation_evidence IS NOT NULL "
            f"RETURN {dist('size(f.validation_evidence)')}",
        ),
        "validated_by_status": r.rows(
            "f:validated",
            f + f"RETURN f.status AS status,{validated} AS validated,count(*) AS n ORDER BY n DESC",
        ),
        "by_relation": r.rows(
            "f:relation",
            f + "RETURN f.relation AS relation,count(*) AS n,"
            f"sum(CASE WHEN {validated} THEN 1 ELSE 0 END) AS validated,"
            "sum(CASE WHEN f.status='uncertain' THEN 1 ELSE 0 END) AS uncertain "
            "ORDER BY n DESC LIMIT 60",
        ),
        "distinct_relations": r.one("f:nrel", f + "RETURN count(DISTINCT f.relation) AS n")["n"],
        "confidence": r.one("f:conf", f + f"RETURN {dist('f.confidence')}"),
        "confidence_buckets": r.table(
            "f:confbuckets",
            f + "RETURN round(coalesce(f.confidence,-1)*10)/10 AS key,count(*) AS n ORDER BY key",
        ),
        "cited_messages_per_fact": r.one("f:cites", f + f"RETURN {dist('size(f.message_refs)')}"),
        "validation_messages_per_fact": r.one(
            "f:valmsgs", f + f"WHERE {validated} RETURN {dist('size(f.validation_message_refs)')}"
        ),
        "valid_at_present": r.table(
            "f:validat", f + "RETURN f.valid_at IS NOT NULL AS key,count(*) AS n"
        ),
        "valid_at_by_month": r.table(
            "f:validmonth",
            f + "WHERE f.valid_at IS NOT NULL "
            "RETURN substring(f.valid_at,0,7) AS key,count(*) AS n ORDER BY key",
        ),
        "recorded_by_day": r.table(
            "f:recorded",
            f + "RETURN substring(f.recorded_at,0,10) AS key,count(*) AS n ORDER BY key",
        ),
        "per_episode": r.one(
            "f:perep", f + f"WITH f.episode_id AS e,count(*) AS c RETURN {dist('c')}"
        ),
        "per_subject": r.one(
            "f:persubject", f + f"WITH f.subject_id AS s,count(*) AS c RETURN {dist('c')}"
        ),
        "per_target": r.one(
            "f:pertarget",
            f + "WHERE f.target_id IS NOT NULL "
            f"WITH f.target_id AS t,count(*) AS c RETURN {dist('c')}",
        ),
        "name_chars": r.one("f:namelen", f + f"RETURN {dist('size(f.name)')}"),
        "summary_chars": r.one(
            "f:sumlen", f + f"WHERE f.summary IS NOT NULL RETURN {dist('size(f.summary)')}"
        ),
        "duplicate_triples": r.one(
            "f:dups",
            f + "WHERE NOT f.retracted "
            "WITH f.subject_id AS s,f.relation AS rel,f.target_id AS t,count(*) AS c WHERE c>1 "
            "RETURN count(*) AS groups,sum(c) AS facts,max(c) AS largest",
        ),
        "without_cites_edge": r.one(
            "f:nocites", f + "WHERE NOT (f)-[:CITES]->() RETURN count(*) AS n"
        )["n"],
        "without_episode_edge": r.one(
            "f:noep", f + "WHERE NOT (f)-[:SUPPORTED_BY]->() RETURN count(*) AS n"
        )["n"],
        "validated_by_source_type": r.table(
            "f:valtype",
            f + "MATCH (f)-[:VALIDATED_BY]->(m:MemoryMessage) "
            "RETURN m.source_type AS key,count(DISTINCT f) AS n",
        ),
        "validated_by_tool": r.table(
            "f:valtool",
            f + "MATCH (f)-[:VALIDATED_BY]->(m:MemoryMessage) "
            "RETURN coalesce(m.tool_name,'(none)') AS key,count(DISTINCT f) AS n "
            "ORDER BY n DESC LIMIT 40",
        ),
        "cited_source_types": r.table(
            "f:citetype",
            f + "MATCH (f)-[:CITES]->(m:MemoryMessage) RETURN m.source_type AS key,count(*) AS n",
        ),
        "status_by_cited_claim": r.rows(
            "f:claimtype",
            f + "MATCH (f)-[:CITES]->(m:MemoryMessage) "
            "WHERE m.source_type IN ['user_assertion','assistant_report'] "
            "WITH f,collect(DISTINCT m.source_type) AS kinds "
            "RETURN kinds,f.status AS status,count(*) AS n ORDER BY n DESC",
        ),
    }
    if names:
        out["top_slots"] = r.table(
            "f:topslots",
            f
            + "WHERE f.slot IS NOT NULL RETURN f.slot AS key,count(*) AS n ORDER BY n DESC LIMIT 40",
        )
        out["top_subjects"] = r.rows(
            "f:topsubjects",
            f + "RETURN f.subject AS subject,count(*) AS n ORDER BY n DESC LIMIT 40",
        )
        out["top_targets"] = r.rows(
            "f:toptargets",
            f + "WHERE f.target IS NOT NULL RETURN f.target AS target,count(*) AS n "
            "ORDER BY n DESC LIMIT 40",
        )
    return out


def entities(r, names):
    e = "MATCH (e:MemoryEntity {namespace:$ns}) "
    out = {
        "total": r.one("en:count", e + "RETURN count(*) AS n")["n"],
        "by_kind": r.table("en:kind", e + "RETURN e.kind AS key,count(*) AS n ORDER BY n DESC"),
        "aliases_per_entity": r.one(
            "en:aliases", e + f"RETURN {dist('size(coalesce(e.aliases,[]))')}"
        ),
        "facts_as_subject": r.one(
            "en:subject", e + f"WITH count{{(e)-[:HAS_FACT]->()}} AS c RETURN {dist('c')}"
        ),
        "facts_as_target": r.one(
            "en:target", e + f"WITH count{{(e)<-[:TARGET]-()}} AS c RETURN {dist('c')}"
        ),
        "without_facts": r.one(
            "en:nofacts",
            e + "WHERE NOT (e)-[:HAS_FACT]->() AND NOT (e)<-[:TARGET]-() RETURN count(*) AS n",
        )["n"],
        "active_facts_as_subject": r.one(
            "en:active",
            e + "WITH count{(e)-[:HAS_FACT]->(:MemoryFact {status:'active'})} AS c "
            f"RETURN {dist('c')}, sum(CASE WHEN c=0 THEN 1 ELSE 0 END) AS none",
        ),
        "name_chars": r.one("en:namelen", e + f"RETURN {dist('size(e.name)')}"),
        "case_insensitive_duplicate_names": r.one(
            "en:dupnames",
            e + "WITH toLower(e.name) AS k,count(*) AS c WHERE c>1 "
            "RETURN count(*) AS groups,sum(c) AS entities",
        ),
        "aliases": {
            "nodes_by_kind": r.table(
                "al:kind",
                "MATCH (a:MemoryAlias {namespace:$ns}) RETURN a.kind AS key,count(*) AS n",
            ),
            "loose": r.table(
                "al:loose",
                "MATCH (a:MemoryAlias {namespace:$ns}) RETURN a.loose AS key,count(*) AS n",
            ),
            "alias_of_edges": r.one(
                "al:edges",
                "MATCH (:MemoryAlias)-[:ALIAS_OF]->(e:MemoryEntity {namespace:$ns}) "
                "RETURN count(*) AS n,count(DISTINCT e) AS entities",
            ),
        },
    }
    if names:
        out["top_by_facts"] = r.rows(
            "en:top",
            e + "WITH e,count{(e)-[:HAS_FACT]->()}+count{(e)<-[:TARGET]-()} AS c "
            "RETURN e.name AS name,e.kind AS kind,c AS facts ORDER BY c DESC LIMIT 60",
        )
    return out


def messages(r):
    m = "MATCH (m:MemoryMessage {namespace:$ns}) "
    return {
        "by_source_type": r.table("m:type", m + "RETURN m.source_type AS key,count(*) AS n"),
        "by_role": r.table("m:role", m + "RETURN m.role AS key,count(*) AS n"),
        "by_role_and_source_type": r.rows(
            "m:roletype",
            m + "RETURN m.role AS role,m.source_type AS source_type,count(*) AS n ORDER BY n DESC",
        ),
        "top_tools": r.rows(
            "m:tools",
            m + "WHERE m.tool_name IS NOT NULL "
            "RETURN m.tool_name AS tool,m.source_type AS source_type,count(*) AS n "
            "ORDER BY n DESC LIMIT 60",
        ),
        "distinct_tools": r.one("m:ntools", m + "RETURN count(DISTINCT m.tool_name) AS n")["n"],
        "tool_failed_by_source_type": r.rows(
            "m:failed",
            m + "WHERE m.tool_failed IS NOT NULL "
            "RETURN m.source_type AS source_type,m.tool_failed AS failed,count(*) AS n",
        ),
        "gaps": r.table(
            "m:gaps", m + "UNWIND coalesce(m.gaps,[]) AS g RETURN g AS key,count(*) AS n"
        ),
        "gap_count_per_message": r.table(
            "m:gapcount", m + "RETURN size(coalesce(m.gaps,[])) AS key,count(*) AS n"
        ),
        "content_chars_by_source_type": {
            row["source_type"]: {k: v for k, v in row.items() if k != "source_type"}
            for row in r.rows(
                "m:chars",
                m + f"RETURN m.source_type AS source_type,{dist('size(m.content)')} "
                "ORDER BY source_type",
            )
        },
        "timestamped": r.table("m:ts", m + "RETURN m.timestamp IS NOT NULL AS key,count(*) AS n"),
        "by_month": r.table(
            "m:month",
            m + "WHERE m.timestamp IS NOT NULL "
            "RETURN substring(m.timestamp,0,7) AS key,count(*) AS n ORDER BY key",
        ),
        "timestamp_range": r.one(
            "m:range", m + "RETURN min(m.timestamp) AS first,max(m.timestamp) AS last"
        ),
        "cited_by_facts_by_source_type": r.table(
            "m:cited", m + "WHERE (m)<-[:CITES]-() RETURN m.source_type AS key,count(*) AS n"
        ),
        "validating_by_source_type": r.table(
            "m:validating",
            m + "WHERE (m)<-[:VALIDATED_BY]-() RETURN m.source_type AS key,count(*) AS n",
        ),
        "episodes_per_message": r.table(
            "m:episodes", m + "RETURN count{(m)<-[:CONTAINS]-()} AS key,count(*) AS n ORDER BY key"
        ),
        "tool_calls_without_result": r.one(
            "m:unpaired",
            m + "WHERE m.source_type='tool_call' AND NOT (m)<-[:RESULT_OF]-() RETURN count(*) AS n",
        )["n"],
        "results_without_call": r.one(
            "m:orphanresult",
            m + "WHERE m.role='tool' AND NOT (m)-[:RESULT_OF]->() RETURN count(*) AS n",
        )["n"],
        "per_session": r.one(
            "m:persession", m + f"WITH m.session_ref AS s,count(*) AS c RETURN {dist('c')}"
        ),
        "record_ids_present": r.table(
            "m:record", m + "RETURN m.record_id IS NOT NULL AS key,count(*) AS n"
        ),
    }


def sessions(r, names):
    s = "MATCH (s:MemorySession {namespace:$ns}) "
    out = {
        "total": r.one("s:count", s + "RETURN count(*) AS n")["n"],
        "messages": r.one(
            "s:msgs", s + f"WITH count{{(s)-[:HAS_MESSAGE]->()}} AS c RETURN {dist('c')}"
        ),
        "episodes": r.one(
            "s:eps", s + f"WITH count{{(s)-[:HAS_EPISODE]->()}} AS c RETURN {dist('c')}"
        ),
        "facts": r.one(
            "s:facts",
            s + "OPTIONAL MATCH (s)-[:HAS_EPISODE]->(e)<-[:SUPPORTED_BY]-(f) "
            f"WITH s,count(f) AS c RETURN {dist('c')}, sum(CASE WHEN c=0 THEN 1 ELSE 0 END) AS without_facts",
        ),
        "validated_facts": r.one(
            "s:validated",
            s + "OPTIONAL MATCH (s)-[:HAS_EPISODE]->(e)<-[:SUPPORTED_BY]-(f) "
            "WHERE size(coalesce(f.validation_message_refs,[]))>0 "
            f"WITH s,count(f) AS c RETURN {dist('c')}, sum(CASE WHEN c=0 THEN 1 ELSE 0 END) AS without_validated",
        ),
        "without_messages": r.one(
            "s:empty", s + "WHERE NOT (s)-[:HAS_MESSAGE]->() RETURN count(*) AS n"
        )["n"],
        "by_source_kind": r.table(
            "s:kind",
            s + "WITH split(coalesce(s.source_uri,''),'/') AS p "
            "RETURN CASE WHEN size(p)>2 THEN p[2] ELSE '(none)' END AS key,count(*) AS n",
        ),
    }
    if names:
        out["by_uri_root"] = r.table(
            "s:root",
            s + "WITH split(coalesce(s.source_uri,''),'/') AS p "
            "RETURN reduce(a='',x IN p[0..4]|a+'/'+x) AS key,count(*) AS n ORDER BY n DESC",
        )
    return out


def feeds(r):
    f = "MATCH (f:MemoryFeed {namespace:$ns}) "
    return {
        "total": r.one("fd:count", f + "RETURN count(*) AS n")["n"],
        "by_source_format": r.table("fd:format", f + "RETURN f.source_format AS key,count(*) AS n"),
        "caught_up": r.table(
            "fd:caught", f + "RETURN f.caught_up_size IS NOT NULL AS key,count(*) AS n"
        ),
        "with_source_key": r.table(
            "fd:key", f + "RETURN f.source_key IS NOT NULL AS key,count(*) AS n"
        ),
        "with_prefix_hash": r.table(
            "fd:hash", f + "RETURN f.prefix_hash IS NOT NULL AS key,count(*) AS n"
        ),
        "message_count": r.one("fd:msgs", f + f"RETURN {dist('coalesce(f.message_count,0)')}"),
        "caught_up_bytes": r.one(
            "fd:bytes", f + f"WHERE f.caught_up_size IS NOT NULL RETURN {dist('f.caught_up_size')}"
        ),
    }


def artifacts(r):
    o = "MATCH (o:MemoryArtifactObservation {namespace:$ns}) "
    return {
        "artifacts": r.one(
            "ar:count", "MATCH (a:MemoryArtifact {namespace:$ns}) RETURN count(*) AS n"
        )["n"],
        "observations": r.one("ar:obs", o + "RETURN count(*) AS n")["n"],
        "by_operation": r.table("ar:op", o + "RETURN o.operation AS key,count(*) AS n"),
        "captured": r.table("ar:captured", o + "RETURN o.captured AS key,count(*) AS n"),
        "gaps": r.table("ar:gaps", o + "RETURN coalesce(o.gap,'(none)') AS key,count(*) AS n"),
        "content_chars": r.one(
            "ar:chars", o + f"WHERE o.content IS NOT NULL RETURN {dist('size(o.content)')}"
        ),
        "observations_per_artifact": r.one(
            "ar:perart",
            "MATCH (a:MemoryArtifact {namespace:$ns}) "
            f"WITH count{{(a)<-[:VERSION_OF]-()}} AS c RETURN {dist('c')}",
        ),
        "by_extension": r.table(
            "ar:ext",
            "MATCH (a:MemoryArtifact {namespace:$ns}) "
            "WITH split(a.path,'.') AS p RETURN CASE WHEN size(p)>1 THEN p[-1] ELSE '(none)' END "
            "AS key,count(*) AS n ORDER BY n DESC LIMIT 30",
        ),
        "absolute_paths": r.table(
            "ar:abs",
            "MATCH (a:MemoryArtifact {namespace:$ns}) "
            "RETURN a.path STARTS WITH '/' AS key,count(*) AS n",
        ),
    }


def journal(r):
    c = "MATCH (c:MemoryChange {scope:$ns}) "
    space = r.one("space", "MATCH (s:MemorySpace {id:$ns}) RETURN s")
    props = {k: v for k, v in (space.get("s") or {}).items() if k != "lock"}
    return {
        "space": props,
        "changes": r.one(
            "j:changes",
            c + "RETURN count(*) AS n,min(c.sequence) AS first_sequence,"
            "max(c.sequence) AS last_sequence,min(c.recorded_at) AS first,max(c.recorded_at) AS last,"
            "sum(size(c.payload)) AS payload_chars",
        ),
        "changes_by_kind": r.table(
            "j:kind", c + "RETURN c.kind AS key,count(*) AS n ORDER BY n DESC"
        ),
        "changes_by_scope": r.table("j:scope", c + "RETURN c.scope AS key,count(*) AS n"),
        "changes_by_day": r.table(
            "j:day", c + "RETURN substring(c.recorded_at,0,10) AS key,count(*) AS n ORDER BY key"
        ),
        "payload_chars_by_kind": {
            row["kind"]: {"count": row["count"], "chars": row["chars"]}
            for row in r.rows(
                "j:kindchars",
                c + "RETURN c.kind AS kind,count(*) AS count,sum(size(c.payload)) AS chars",
            )
        },
        "snapshot_parts": r.rows(
            "j:parts",
            "MATCH (p:MemorySnapshotPart {namespace:$ns}) "
            "RETURN p.label AS label,p.sequence AS sequence,count(*) AS parts,"
            "sum(size(p.data)) AS data_chars ORDER BY sequence,label",
        ),
        "workers": r.rows(
            "j:workers",
            "MATCH (w:MemoryWorker {namespace:$ns}) "
            "RETURN w.pid AS pid,w.workers AS workers,w.interval AS interval,w.engine AS engine,"
            "w.provider_open AS provider_open,w.provider_reason AS provider_reason,"
            "w.heartbeat_at AS heartbeat_at",
        ),
        "inventory": r.rows(
            "j:inventory",
            "MATCH (i:MemoryInventory {namespace:$ns}) "
            "RETURN i.name AS name,size(i.payload) AS payload_chars",
        ),
    }


def other_labels(r, labels):
    """Counts and the status-like breakdown of the labels this snapshot does not detail."""
    out = {}
    for label in labels:
        row = r.one(
            f"other:{label}",
            f"MATCH (n:`{label}`) WHERE n.namespace=$ns OR n.id=$ns RETURN count(*) AS n,"
            "collect(DISTINCT n.status)[..20] AS statuses,collect(DISTINCT n.kind)[..20] AS kinds",
        )
        out[label] = row
    return out


def integrity(r):
    return {
        "facts_citing_missing_messages": r.one(
            "i:missingcite",
            "MATCH (f:MemoryFact {namespace:$ns}) UNWIND f.message_refs AS mid "
            "OPTIONAL MATCH (m:MemoryMessage {id:mid}) WITH f,mid,m WHERE m IS NULL "
            "RETURN count(DISTINCT f) AS n",
        )["n"],
        "facts_validated_by_context_messages": r.one(
            "i:contextval",
            "MATCH (f:MemoryFact {namespace:$ns})-[:VALIDATED_BY]->(m) "
            "WHERE m.source_type='context' RETURN count(DISTINCT f) AS n",
        )["n"],
        "facts_validated_by_failed_tool": r.one(
            "i:failedval",
            "MATCH (f:MemoryFact {namespace:$ns})-[:VALIDATED_BY]->(m) "
            "WHERE m.tool_failed = true RETURN count(DISTINCT f) AS n",
        )["n"],
        "facts_without_subject_entity": r.one(
            "i:nosubject",
            "MATCH (f:MemoryFact {namespace:$ns}) "
            "WHERE NOT (:MemoryEntity {id:f.subject_id})-[:HAS_FACT]->(f) RETURN count(*) AS n",
        )["n"],
        "facts_with_target_but_no_edge": r.one(
            "i:notarget",
            "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.target_id IS NOT NULL "
            "AND NOT (f)-[:TARGET]->() RETURN count(*) AS n",
        )["n"],
        "facts_whose_episode_is_missing": r.one(
            "i:noepisode",
            "MATCH (f:MemoryFact {namespace:$ns}) "
            "WHERE NOT EXISTS {MATCH (e:MemoryEpisode {id:f.episode_id})} RETURN count(*) AS n",
        )["n"],
        "messages_without_session": r.one(
            "i:nosession",
            "MATCH (m:MemoryMessage {namespace:$ns}) "
            "WHERE NOT (m)<-[:HAS_MESSAGE]-() RETURN count(*) AS n",
        )["n"],
        "messages_in_no_episode": r.one(
            "i:noep",
            "MATCH (m:MemoryMessage {namespace:$ns}) "
            "WHERE NOT (m)<-[:CONTAINS]-() RETURN count(*) AS n",
        )["n"],
        "episodes_without_session": r.one(
            "i:epnosession",
            "MATCH (e:MemoryEpisode {namespace:$ns}) "
            "WHERE NOT (e)<-[:HAS_EPISODE]-() RETURN count(*) AS n",
        )["n"],
        "nodes_without_namespace": r.table(
            "i:nons", "MATCH (n) WHERE n.namespace IS NULL RETURN labels(n)[0] AS key,count(*) AS n"
        ),
    }


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def docker(prefix):
    out = {"containers": {}}
    names = [
        n
        for n in run(["docker", "ps", "-a", "--format", "{{.Names}}"]).split()
        if n.startswith(prefix)
    ]
    for name in names:
        info = json.loads(run(["docker", "inspect", name]))[0]
        env = dict(kv.split("=", 1) for kv in info["Config"].get("Env", []) if "=" in kv)
        out["containers"][name] = {
            "image": info["Config"]["Image"],
            "image_id": info["Image"],
            "state": info["State"]["Status"],
            "health": (info["State"].get("Health") or {}).get("Status"),
            "started_at": info["State"]["StartedAt"],
            "restarts": info["RestartCount"],
            "command": info["Config"].get("Cmd"),
            "memory_limit": info["HostConfig"].get("Memory"),
            "env": {
                k: v
                for k, v in sorted(env.items())
                if not any(s in k.upper() for s in SECRET)
                and (
                    k.startswith(
                        ("MEMORY_", "NEO4J_", "TRANSCRIPT", "GIT_", "GRAPH_", "CODEX", "LLM")
                    )
                )
            },
        }
    stats = run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"]
    )
    out["stats"] = {
        line.split("\t")[0]: {"memory": line.split("\t")[1], "cpu": line.split("\t")[2]}
        for line in stats.splitlines()
        if line.startswith(prefix)
    }
    neo4j = f"{prefix}-neo4j-1"
    if neo4j in names:
        out["store"] = {
            line.split("\t")[1]: line.split("\t")[0]
            for line in run(
                [
                    "docker",
                    "exec",
                    neo4j,
                    "du",
                    "-sh",
                    "/data/databases/neo4j",
                    "/data/transactions/neo4j",
                ]
            ).splitlines()
            if "\t" in line
        }
    return out


def worker_log(prefix):
    """Every processed episode since the worker container started: outcomes and timings."""
    name = f"{prefix}-worker-1"
    lines = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
    events, processed = Counter(), []
    for line in (lines.stdout + lines.stderr).splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if "event" in event:
            events[event["event"]] += 1
        if event.get("event") == "processed":
            processed.append(event)
    by_status = Counter(e.get("status") for e in processed)
    errors = Counter(e.get("error") for e in processed if e.get("error"))
    hours = Counter(datetime.fromtimestamp(e["ts"], UTC).strftime("%Y-%m-%dT%H") for e in processed)
    timings = defaultdict(list)
    for e in processed:
        for k, v in (e.get("timings") or {}).items():
            timings[k].append(v)
    modelled = [e for e in processed if e.get("model_calls")]

    def summary(values):
        if not values:
            return {}
        s = sorted(values)
        pick = lambda q: s[min(len(s) - 1, int(q * len(s)))]  # noqa: E731
        return {
            "count": len(s),
            "min": s[0],
            "p50": pick(0.5),
            "p90": pick(0.9),
            "p99": pick(0.99),
            "max": s[-1],
            "avg": round(sum(s) / len(s), 3),
            "sum": round(sum(s), 1),
        }

    return {
        "events": dict(events.most_common()),
        "processed": len(processed),
        "by_status": dict(by_status),
        "errors": dict(errors.most_common(20)),
        "with_model_call": len(modelled),
        "model_calls_total": sum(e.get("model_calls") or 0 for e in processed),
        "per_hour_utc": dict(sorted(hours.items())),
        "timings_seconds": {k: summary(v) for k, v in timings.items()},
        "model_seconds_when_called": summary(
            [e["timings"]["model"] for e in modelled if "timings" in e]
        ),
        "first_ts": min((e["ts"] for e in processed), default=None),
        "last_ts": max((e["ts"] for e in processed), default=None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="transcripts")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--names",
        action="store_true",
        help="Include entity, subject and path names. Keep such a report out of the repository.",
    )
    parser.add_argument("--container-prefix", default=None)
    args = parser.parse_args()
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD") or ""),
        notifications_min_severity="OFF",
    )
    r = Reader(driver, os.environ.get("NEO4J_DATABASE", "neo4j"), args.namespace)
    started = time.monotonic()
    report = {
        "kind": "graph_baseline_counts_only" + ("_with_names" if args.names else ""),
        "captured_at": datetime.now(UTC).isoformat(),
        "namespace": args.namespace,
        "commit": run(["git", "rev-parse", "HEAD"]).strip(),
        "engine": engine_fingerprint(),
    }
    report["schema"] = schema(r)
    labels, rel_types = report["schema"]["labels"], report["schema"]["relationship_types"]
    report["nodes"] = nodes(r, labels, rel_types)
    report["episodes"] = episodes(r)
    report["facts"] = facts(r, args.names)
    report["entities"] = entities(r, args.names)
    report["messages"] = messages(r)
    report["sessions"] = sessions(r, args.names)
    report["feeds"] = feeds(r)
    report["artifacts"] = artifacts(r)
    report["journal"] = journal(r)
    detailed = {
        "MemoryEpisode",
        "MemoryFact",
        "MemoryEntity",
        "MemoryMessage",
        "MemorySession",
        "MemoryFeed",
        "MemoryArtifact",
        "MemoryArtifactObservation",
        "MemoryChange",
        "MemorySnapshotPart",
        "MemoryWorker",
        "MemoryInventory",
        "MemorySpace",
        "MemoryAlias",
    }
    report["other_labels"] = other_labels(r, [x for x in labels if x not in detailed])
    report["integrity"] = integrity(r)
    if args.container_prefix:
        report["deployment"] = docker(args.container_prefix)
        report["worker_log"] = worker_log(args.container_prefix)
    report["capture"] = {
        "seconds": round(time.monotonic() - started, 1),
        "slowest_queries": dict(sorted(r.timings.items(), key=lambda kv: -kv[1])[:15]),
        "basis": "Read-only aggregate queries against the live graph while it may be ingesting; "
        "every number is as of its own query, not one transaction. Docker figures are of the "
        "moment; the worker log covers only the current container's lifetime.",
    }
    driver.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(args.output), **report["capture"]}))


if __name__ == "__main__":
    main()
