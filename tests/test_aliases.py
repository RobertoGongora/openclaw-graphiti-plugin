import pytest

from graph_memory import aliases
from graph_memory import retrieval as v
from graph_memory.journal import Journal
from graph_memory.models import Entity, Transcript
from graph_memory.store import normalized
from tests.helpers import MYSQL, PG, PROJECT, ingest

SEED = [
    {"key": "project:atlas", "name": "Atlas", "kind": "project", "aliases": ["Atlas API", "n8n"]},
    {"key": "project:other-atlas", "name": "Atlas", "kind": "project"},
    {"key": "person:ana-a", "name": "Ana", "kind": "person"},
    {"key": "person:ana-b", "name": "Ana", "kind": "person"},
    {"key": "database:mysql", "name": "MySQL", "kind": "database"},
    {"key": "database:maria", "name": "MariaDB", "kind": "database", "aliases": ["maria"]},
    {"key": "event:launch", "name": "Launch", "kind": "event"},
    {"key": "language:cpp", "name": "C++", "kind": "language", "aliases": [".NET", "API"]},
]
PROBES = [
    {"key": "project:atlas", "name": "Atlas", "kind": "project"},
    {"key": "project:third", "name": "Atlas", "kind": "project"},
    {"key": "project:third", "name": "Third", "kind": "project", "aliases": ["project:atlas"]},
    {"key": "person:ana-c", "name": "Ana", "kind": "person"},
    {
        "key": "person:ana-c",
        "name": "Ana",
        "kind": "person",
        "aliases": ["person:ana-a", "person:ana-b"],
    },
    {"key": "database:maria", "name": "Maria", "kind": "database"},
    {"key": "database:other", "name": "MARIA", "kind": "database"},
    {"key": "event:launch-2", "name": "Launch", "kind": "event"},
    {"key": "event:launch", "name": "Relaunch", "kind": "event"},
    {"key": "language:dotnet", "name": ".net", "kind": "language"},
    {"key": "database:atlas", "name": "Atlas", "kind": "database"},
]


def seed(store, ns):
    def run(tx):
        store.lock(tx, ns)
        for raw in SEED:
            store.canonical(tx, ns, Entity.model_validate(raw))

    store.transaction(run)
    store.merge(ns, "database:maria", "database:mysql", "same server")


def strip(store, ns):
    """The namespace as it was before alias nodes existed."""
    store.transaction(
        lambda tx: tx.run(
            "MATCH (s:MemorySpace {id:$ns}) REMOVE s.aliases_indexed "
            "WITH s MATCH (a:MemoryAlias {namespace:$ns}) DETACH DELETE a",
            ns=ns,
        ).consume()
    )


def alias_graph(store, ns):
    return store.read(
        lambda tx: tx.run(
            "MATCH (a:MemoryAlias {namespace:$ns}) OPTIONAL MATCH (a)-[:ALIAS_OF]->(e) "
            "RETURN a.id AS id,a.kind AS kind,a.text AS text,a.loose AS loose,e.id AS entity "
            "ORDER BY id,entity",
            ns=ns,
        ).data()
    )


def identities(store, ns):
    """What canonical decides for every probe, each judged alone and rolled back."""
    out = []
    for raw in PROBES:
        with store.driver.session() as session, session.begin_transaction() as tx:
            store.lock(tx, ns)
            try:
                out.append(store.canonical(tx, ns, Entity.model_validate(raw)))
            except ValueError as error:
                out.append(str(error))
            tx.rollback()
    return out


def transcript(ns, text):
    return Transcript.model_validate(
        {
            "namespace": ns,
            "source_id": "s",
            "session_id": "s",
            "messages": [{"id": "m1", "role": "user", "content": text}],
        }
    )


def mentioned(store, ns, text):
    return [row["key"] for row in store.extraction_context(ns, transcript(ns, text))]


def test_canonical_decides_the_same_with_and_without_alias_nodes(graph):
    store, ns = graph
    seed(store, ns)
    assert store.read(lambda tx: aliases.indexed(tx, ns))
    native = identities(store, ns)
    assert sum("Ambiguous" in str(r) for r in native) == 1
    mysql = native[5]
    assert mysql[1] == "database:mysql" and native[6] == mysql
    assert native[7][1] == "event:launch-2" and native[8][1] == "event:launch"
    strip(store, ns)
    assert not store.read(lambda tx: aliases.indexed(tx, ns))
    assert identities(store, ns) == native
    aliases.rebuild(store, ns)
    assert identities(store, ns) == native


def test_extraction_context_matches_whole_words(graph):
    store, ns = graph
    seed(store, ns)
    assert mentioned(store, ns, "The capital of Spain and a rapid map.") == []
    assert mentioned(store, ns, "We shipped the Atlas   API today") == [
        "language:cpp",
        "project:atlas",
        "project:other-atlas",
    ]
    assert mentioned(store, ns, "the API, finally") == ["language:cpp"]
    rows = store.extraction_context(ns, transcript(ns, "n8n broke"))
    assert [set(r) for r in rows] == [{"key", "kind", "name", "aliases"}]
    assert rows[0]["key"] == "project:atlas" and "atlas api" in rows[0]["aliases"]
    # Merged away: its names now lead to the entity it became.
    assert mentioned(store, ns, "MariaDB is down") == ["database:mysql"]


def test_extraction_context_matches_names_with_punctuation(graph):
    store, ns = graph
    seed(store, ns)
    assert mentioned(store, ns, "Work on (project:atlas), please") == [
        "project:atlas",
        "project:other-atlas",
    ]
    assert mentioned(store, ns, "rewrite it in C++, not .NET") == ["language:cpp"]
    assert mentioned(store, ns, "rewrite it in c+") == []
    assert aliases.loose("c++") and aliases.loose(".net") and not aliases.loose("project:atlas")
    assert aliases.loose("one two three four five six seven")


def test_a_long_text_reads_the_names_once_and_finds_the_same(graph, monkeypatch):
    store, ns = graph
    seed(store, ns)
    text = "rewrite (project:atlas) in C++; the capital API of MariaDB"
    sought = mentioned(store, ns, text)
    assert sought == ["database:mysql", "language:cpp", "project:atlas", "project:other-atlas"]
    monkeypatch.setattr(aliases, "SWEEP", 0)
    assert mentioned(store, ns, text) == sought


def test_extraction_context_keeps_the_longest_names_when_capped(graph):
    store, ns = graph

    def run(tx):
        store.lock(tx, ns)
        for i in range(12):
            raw = {"key": f"topic:t{i:02}", "name": "tool " + "x" * i, "kind": "topic"}
            store.canonical(tx, ns, Entity.model_validate(raw))

    store.transaction(run)
    text = " and ".join("tool " + "x" * i for i in range(12)) + " and tool xx"
    kept = store.read(lambda tx: aliases.mentioned(tx, ns, normalized(text), limit=3))
    assert [r["key"] for r in kept] == ["topic:t09", "topic:t10", "topic:t11"]


def test_merge_repoints_aliases(graph):
    store, ns = graph
    seed(store, ns)
    owners = store.read(
        lambda tx: tx.run(
            "MATCH (a:MemoryAlias {namespace:$ns})-[:ALIAS_OF]->(e) WHERE a.kind='database' "
            "RETURN a.text AS text,e.key AS key ORDER BY text",
            ns=ns,
        ).data()
    )
    assert {o["key"] for o in owners} == {"database:mysql"}
    assert [o["text"] for o in owners] == [
        "database:maria",
        "database:mysql",
        "maria",
        "mariadb",
        "mysql",
    ]
    assert [e["key"] for e in store.recall(ns, "maria")["entities"]] == ["database:mysql"]


def test_rebuild_is_idempotent_and_equals_native_writes(graph):
    store, ns = graph
    seed(store, ns)
    native = alias_graph(store, ns)
    assert len(native) == 21 and sum(1 for a in native if a["loose"]) == 2
    assert aliases.rebuild(store, ns)["created"] == 0
    strip(store, ns)
    assert alias_graph(store, ns) == []
    assert aliases.rebuild(store, ns) == {
        "entities": 7,
        "aliases": 21,
        "created": 21,
        "removed": 0,
    }
    assert alias_graph(store, ns) == native
    assert aliases.rebuild(store, ns)["created"] == 0
    assert alias_graph(store, ns) == native
    # Drift in both directions, healed by the explicit repair command.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (a:MemoryAlias {namespace:$ns,text:'n8n'}) DETACH DELETE a "
            "WITH count(*) AS gone MATCH (e:MemoryEntity {namespace:$ns,key:'event:launch'}) "
            "CREATE (:MemoryAlias {id:'stray',namespace:$ns,kind:'event',text:'gone'})"
            "-[:ALIAS_OF]->(e)",
            ns=ns,
        ).consume()
    )
    assert alias_graph(store, ns) != native
    store.repair(ns)
    assert alias_graph(store, ns) == native


def test_rebuild_commits_in_batches(graph, monkeypatch):
    store, ns = graph
    seed(store, ns)
    native = alias_graph(store, ns)
    strip(store, ns)
    monkeypatch.setattr(aliases, "BATCH", 4)
    assert aliases.rebuild(store, ns)["created"] == 21
    assert alias_graph(store, ns) == native


def test_reads_fall_back_to_the_list_scan_before_migration(graph):
    store, ns = graph
    seed(store, ns)

    def search(query):
        found = v.search_entities(store, v.EntitySearch(namespace=ns, query=query))
        return found["total"], [m["key"] for m in found["matches"]]

    indexed = (
        store.recall(ns, "atlas")["entities"],
        store.recall(ns, "ana")["ambiguous"],
        search("atla"),
        search("maria"),
    )
    assert indexed[2] == (2, ["project:atlas", "project:other-atlas"])
    strip(store, ns)
    assert (
        store.recall(ns, "atlas")["entities"],
        store.recall(ns, "ana")["ambiguous"],
        search("atla"),
        search("maria"),
    ) == indexed
    # The old substring match, kept only until the migration has run.
    assert mentioned(store, ns, "The capital of Spain") == ["language:cpp"]

    def run(tx):
        store.lock(tx, ns)
        return store.canonical(tx, ns, Entity.model_validate(PROBES[0]))

    assert store.transaction(run)[1] == "project:atlas"
    assert not store.read(lambda tx: aliases.indexed(tx, ns))
    aliases.rebuild(store, ns)
    assert mentioned(store, ns, "The capital of Spain") == []


def test_search_entities_pages_on_the_server(graph):
    store, ns = graph
    seed(store, ns)

    def search(query, **kw):
        return v.search_entities(store, v.EntitySearch(namespace=ns, query=query, **kw))

    first, second = search("Atlas", limit=1), search("atlas", limit=1, offset=1)
    assert first["total"] == second["total"] == 2
    assert first["matches"][0]["key"] == "project:atlas" and first["next_offset"] == 1
    assert second["matches"][0]["key"] == "project:other-atlas"
    assert second["next_offset"] is None
    assert search("atlas launch")["matches"] == []
    assert search("api atlas")["matches"][0]["match"] == "words"
    assert search("Atlas", kind="person")["total"] == 0
    assert search("maria")["matches"][0]["key"] == "database:mysql"


def test_alias_nodes_stay_outside_the_journal(graph):
    store, ns = graph
    base = {"relation": "uses_database", "slot": "primary", "valid_at": "2026-09-14T10:00:00Z"}
    project = {**PROJECT, "aliases": ["Atlas API"]}
    ingest(
        store,
        ns,
        "a",
        "Atlas uses MySQL.",
        [project, MYSQL],
        [{**base, "subject": project["key"], "target": MYSQL["key"]}],
    )
    ingest(
        store,
        ns,
        "b",
        "Atlas uses Postgres.",
        [PROJECT, PG],
        [{**base, "subject": PROJECT["key"], "target": PG["key"]}],
        timestamp="2026-09-15T10:00:00Z",
    )
    store.merge(ns, PG["key"], MYSQL["key"], "test")
    store.repair(ns)
    strip(store, ns)
    aliases.rebuild(store, ns)
    assert len(alias_graph(store, ns)) == 8
    journal = Journal(store)
    assert journal.verify(ns)["verified"] and journal.verify_live(ns)["verified"]
    replayed = journal.replay(ns, "replay:" + ns)["namespace"]
    try:
        assert store.read(lambda tx: aliases.indexed(tx, replayed))
        assert [t["text"] for t in alias_graph(store, replayed)].count("postgresql") == 1
        assert [e["key"] for e in store.recall(replayed, "postgresql")["entities"]] == [
            MYSQL["key"]
        ]
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) "
                "OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=replayed,
            ).consume()
        )


def operators(plan):
    yield plan["operatorType"].split("@")[0]
    for child in plan.get("children", []):
        yield from operators(child)


@pytest.mark.parametrize(
    ("query", "seek"),
    [
        (aliases.RESOLVE, "NodeIndexSeek"),
        (aliases.MENTIONED, "NodeIndexSeek"),
        (aliases.CONTAINING, "NodeIndexContainsScan"),
    ],
)
def test_lookups_are_index_seeks(graph, query, seek):
    store, ns = graph
    seed(store, ns)
    with store.driver.session() as session:
        session.run("CALL db.awaitIndexes(60)").consume()
        plan = (
            session.run(
                "EXPLAIN " + query,
                ns=ns,
                kind="project",
                names=["atlas"],
                grams=["atlas"],
                q="atlas",
                limit=21,
            )
            .consume()
            .plan
        )
    assert plan is not None
    found = set(operators(plan))
    assert seek in found, found
    assert not found & {"NodeByLabelScan", "AllNodesScan", "PartitionedNodeByLabelScan"}, found


def test_a_name_inside_a_snake_case_identifier_is_a_mention(graph):
    store, ns = graph
    ingest(
        store,
        ns,
        "one",
        "Atlas API uses MySQL.",
        [{"key": "project:atlas-api", "name": "Atlas API", "kind": "project"}, MYSQL],
        [{"subject": "project:atlas-api", "target": MYSQL["key"], "relation": "uses_database"}],
    )
    from graph_memory.models import Transcript

    def keys(text):
        transcript = Transcript.model_validate(
            {
                "namespace": ns,
                "source_id": "probe",
                "session_id": "probe",
                "messages": [{"id": "m1", "role": "user", "content": text}],
            }
        )
        return {row["key"] for row in store.extraction_context(ns, transcript)}

    assert "project:atlas-api" in keys("export ATLAS_API_KEY=... then call atlas_api_client")
    assert "database:mysql" not in keys("the mysqldump binary is missing")


def test_a_name_too_long_to_index_does_not_fail_the_commit(graph):
    store, ns = graph
    long_name = "ﷺ" * 400  # expands far past the index key limit once normalized
    ingest(
        store,
        ns,
        "one",
        "Atlas uses MySQL.",
        [{**PROJECT, "aliases": [long_name]}, MYSQL],
        [{"subject": PROJECT["key"], "target": MYSQL["key"], "relation": "uses_database"}],
    )
    assert store.recall(ns, "Atlas")["entities"]
