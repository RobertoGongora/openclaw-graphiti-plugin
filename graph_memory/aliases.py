"""Indexed name lookup derived from `MemoryEntity.aliases`.

Neo4j cannot index a list property, so every name is mirrored as
`(:MemoryAlias {id,namespace,kind,text})-[:ALIAS_OF]->(:MemoryEntity)`.
The list stays the journaled source of truth; these nodes are outside the
journal and `rebuild` recreates them from the lists at any time."""

import re
from collections import Counter

from .store import GraphStore, digest

BATCH = 5000
WIDTH = 6
FLOOR = 3
SWEEP = 50_000
# An underscore separates words: a name inside a snake_case identifier
# ("atlas_api_key") is a mention, and losing it makes the extractor coin a new key
# for an entity that already exists.
WORD = re.compile(r"[^\W_]+")
# Neo4j refuses to index a property value over about 8 kB and fails the whole write.
# Counted in characters (at most four bytes each) so a query can apply the same rule.
INDEXABLE = 500


def alias_id(namespace, kind, text, entity_id):
    return digest(["alias", namespace, kind, text, entity_id])


def loose(text):
    """A name the word n-grams of a text can never equal: it starts or ends
    outside a word ("c++", ".net") or is longer than the widest n-gram."""
    spans = [m.span() for m in WORD.finditer(text)]
    return not spans or spans[0][0] != 0 or spans[-1][1] != len(text) or len(spans) > WIDTH


def rows(namespace, kind, entity_id, texts):
    return [
        {
            "id": alias_id(namespace, kind, text, entity_id),
            "entity": entity_id,
            "kind": kind,
            "text": text,
            # Absent rather than false: the (namespace, loose) index holds only these.
            "loose": True if loose(text) else None,
        }
        for text in dict.fromkeys(texts)
        # A name too long to index is left to the alias list; nothing resolves by it.
        if len(text) <= INDEXABLE
    ]


def indexed(tx, namespace):
    row = tx.run(
        "MATCH (s:MemorySpace {id:$ns}) RETURN coalesce(s.aliases_indexed,false) AS indexed",
        ns=namespace,
    ).single()
    return bool(row and row["indexed"])


def claim(tx, namespace):
    """Whether lookups may trust the alias nodes. A namespace with no entities
    has nothing to migrate, so its first write claims it; one that predates
    the alias nodes keeps the list scan until `rebuild` has run."""
    if indexed(tx, namespace):
        return True
    return bool(
        tx.run(
            "MATCH (s:MemorySpace {id:$ns}) "
            "WHERE NOT EXISTS { MATCH (:MemoryEntity {namespace:$ns}) } "
            "SET s.aliases_indexed=true RETURN true AS claimed",
            ns=namespace,
        ).single()
    )


def link(tx, namespace, batch):
    return tx.run(
        "UNWIND $rows AS row MATCH (e:MemoryEntity {id:row.entity}) WHERE e.merged_into IS NULL "
        "MERGE (a:MemoryAlias {id:row.id}) ON CREATE SET a.namespace=$ns,a.kind=row.kind,"
        "a.text=row.text,a.loose=row.loose MERGE (a)-[:ALIAS_OF]->(e) RETURN count(a) AS count",
        ns=namespace,
        rows=batch,
    ).single()["count"]


def repoint(tx, namespace, source, target):
    """A merged-away entity answers to no name; the entity it became answers to all of them."""
    tx.run(
        "MATCH (a:MemoryAlias)-[:ALIAS_OF]->(:MemoryEntity {id:$id}) DETACH DELETE a",
        id=source["id"],
    ).consume()
    link(tx, namespace, rows(namespace, target["kind"], target["id"], source["aliases"]))


def rebuild(target, namespace):
    """Make the alias nodes of a namespace equal to its alias lists. Idempotent.
    Given a store it commits in batches, each under the namespace lock; given a
    transaction it stays inside it, and the caller holds the lock."""

    def each(fn):
        if not isinstance(target, GraphStore):
            return fn(target)

        def locked(tx):
            GraphStore.lock(tx, namespace)
            return fn(tx)

        return target.transaction(locked)

    def survey(tx):
        entities = tx.run(
            "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.merged_into IS NULL "
            "RETURN e.id AS id,e.kind AS kind,coalesce(e.aliases,[]) AS aliases",
            ns=namespace,
        ).data()
        existing = tx.run(
            "MATCH (a:MemoryAlias {namespace:$ns}) RETURN a.id AS id", ns=namespace
        ).value()
        return entities, existing

    entities, existing = each(survey)
    wanted = [r for e in entities for r in rows(namespace, e["kind"], e["id"], e["aliases"])]
    keep = {r["id"] for r in wanted}
    stale = [i for i in existing if i not in keep]
    missing = set(keep).difference(existing)
    create = [r for r in wanted if r["id"] in missing]
    removed = 0
    for start in range(0, len(stale), BATCH):
        removed += each(
            lambda tx, ids=stale[start : start + BATCH]: tx.run(
                # Judged again here: between batches a writer may have made one current.
                "UNWIND $ids AS id MATCH (a:MemoryAlias {id:id}) "
                "OPTIONAL MATCH (a)-[:ALIAS_OF]->(e:MemoryEntity) WITH a,e "
                "WHERE e IS NULL OR e.merged_into IS NOT NULL OR NOT a.text IN e.aliases "
                "DETACH DELETE a RETURN count(*) AS count",
                ids=ids,
            ).single()["count"]
        )
    created = 0
    for start in range(0, len(create), BATCH):
        created += each(lambda tx, batch=create[start : start + BATCH]: link(tx, namespace, batch))
    each(
        lambda tx: tx.run(
            "MATCH (s:MemorySpace {id:$ns}) SET s.aliases_indexed=true", ns=namespace
        ).consume()
    )
    return {
        "entities": len(entities),
        "aliases": len(wanted),
        "created": created,
        "removed": removed,
    }


# The hints are the contract: on a small graph the planner otherwise picks a scan.
RESOLVE = (
    "MATCH (a:MemoryAlias) USING INDEX SEEK a:MemoryAlias(namespace,text) WHERE a.namespace=$ns AND a.text IN $names AND a.kind=$kind "
    "MATCH (a)-[:ALIAS_OF]->(e:MemoryEntity) WHERE e.merged_into IS NULL "
    "WITH DISTINCT e RETURN properties(e) AS e"
)


def resolve(tx, namespace, kind, names):
    """Runs once per extracted entity inside the namespace write lock: it must stay an index seek."""
    return tx.run(
        RESOLVE,
        ns=namespace,
        kind=kind,
        names=names,
    ).data()


MENTIONED = (
    "UNWIND $grams AS g MATCH (a:MemoryAlias {namespace:$ns,text:g})"
    "-[:ALIAS_OF]->(e:MemoryEntity) USING INDEX SEEK a:MemoryAlias(namespace,text) "
    "WHERE e.merged_into IS NULL "
    "RETURN e.id AS id,e.key AS key,g AS text"
)
CONTAINING = (
    "MATCH (a:MemoryAlias) USING TEXT INDEX a:MemoryAlias(text) "
    "WHERE a.namespace=$ns AND a.text CONTAINS $q "
    "MATCH (a)-[:ALIAS_OF]->(e:MemoryEntity) WHERE e.merged_into IS NULL "
    "WITH e,min(CASE WHEN a.text=$q THEN 0 ELSE 1 END) AS rank "
    "RETURN properties(e) AS entity ORDER BY rank,e.key LIMIT $limit"
)


def grams(content):
    """Every run of 1..WIDTH consecutive words, punctuation between them kept,
    with how often it occurs: "project:atlas" and "atlas" both come out of
    "on project:atlas today", and "api" does not come out of "capital"."""
    spans = [m.span() for m in WORD.finditer(content)]
    counts = Counter()
    for i, (start, _) in enumerate(spans):
        for _, end in spans[i : i + WIDTH]:
            if FLOOR <= end - start <= 500:
                gram = content[start:end]
                counts[gram] += 1
                # "atlas_api_key" mentions "atlas api": an identifier spells a name
                # with underscores where prose uses spaces.
                if "_" in gram:
                    counts[gram.replace("_", " ")] += 1
    return counts


def mentioned(tx, namespace, content, limit=200):
    counts = grams(content)
    for text in tx.run(
        "MATCH (a:MemoryAlias) WHERE a.namespace=$ns AND a.loose=true RETURN DISTINCT a.text AS t",
        ns=namespace,
    ).value():
        # No word boundary to anchor these to, so the old substring test stands.
        if len(text) >= FLOOR and text in content:
            counts[text] = content.count(text)
    found = list(counts)
    if len(found) > SWEEP:
        # Past this many seeks, reading every name of the namespace once is cheaper.
        found = [
            text
            for text in tx.run(
                "MATCH (a:MemoryAlias) WHERE a.namespace=$ns AND a.text IS NOT NULL "
                "RETURN DISTINCT a.text AS t",
                ns=namespace,
            ).value()
            if text in counts
        ]
    best, seen = Counter(), Counter()
    for start in range(0, len(found), BATCH):
        for row in tx.run(
            MENTIONED,
            ns=namespace,
            grams=found[start : start + BATCH],
        ):
            ref = (row["id"], row["key"])
            best[ref] = max(best[ref], len(row["text"]))
            seen[ref] += counts[row["text"]]
    # The longest name is the least likely to be an accidental match; mentions break ties.
    ranked = sorted(best, key=lambda ref: (-best[ref], -seen[ref], ref[1]))[:limit]
    return tx.run(
        "MATCH (e:MemoryEntity) WHERE e.id IN $ids "
        "RETURN e.key AS key,e.kind AS kind,e.name AS name,e.aliases AS aliases ORDER BY key",
        ids=[ref[0] for ref in ranked],
    ).data()


def containing(tx, namespace, needle, limit):
    """Entities with a name containing the needle, exact names first."""
    return tx.run(
        CONTAINING,
        ns=namespace,
        q=needle,
        limit=limit,
    ).data()


def search(tx, namespace, needle, terms, kind, offset, limit):
    row = tx.run(
        "CALL { "
        "MATCH (a:MemoryAlias) USING TEXT INDEX a:MemoryAlias(text) "
        "WHERE a.namespace=$ns AND a.text CONTAINS $needle AND ($kind IS NULL OR a.kind=$kind) "
        "MATCH (a)-[:ALIAS_OF]->(e:MemoryEntity) "
        "RETURN e,CASE WHEN a.text=$needle THEN 1 ELSE 2 END AS score "
        "UNION ALL "
        "UNWIND $terms AS term MATCH (a:MemoryAlias) USING TEXT INDEX a:MemoryAlias(text) "
        "WHERE a.namespace=$ns AND a.text CONTAINS term AND ($kind IS NULL OR a.kind=$kind) "
        "MATCH (a)-[:ALIAS_OF]->(e:MemoryEntity) "
        "WITH e,count(DISTINCT term) AS hit WHERE hit=size($terms) RETURN e,3 AS score "
        "} WITH e,min(score) AS score WHERE e.merged_into IS NULL "
        # The key itself outranks any other exact name; the caller ranks the page again.
        "WITH e,CASE WHEN score=1 AND toLower(e.key)=$needle THEN 0 ELSE score END AS score "
        "ORDER BY score,e.key WITH collect(e) AS found "
        "RETURN size(found) AS total,"
        "[e IN found[$offset..$offset+$limit] | properties(e)] AS page",
        ns=namespace,
        needle=needle,
        terms=terms,
        kind=kind,
        offset=offset,
        limit=limit,
    ).single()
    return row["total"], row["page"]
