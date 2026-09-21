"""Direct Neo4j graph repository, atomic commits and durable extraction inbox."""

import hashlib
import json
import re
import threading
import unicodedata
from datetime import datetime

from neo4j import WRITE_ACCESS, GraphDatabase

from .models import Extraction, Transcript, now
from .temporal import project
from .version import engine_fingerprint


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


class GraphStore:
    def __init__(
        self, uri: str, user: str = "neo4j", password: str | None = None, database: str = "neo4j"
    ):
        self.driver = GraphDatabase.driver(
            uri, auth=(user, password) if password else None, notifications_min_severity="OFF"
        )
        self.database = database
        self.engine = engine_fingerprint()
        self._journal_local = threading.local()
        # Model work remains concurrent. Journal writes already serialize on the
        # namespace; avoid making this process's transactions contend for it.
        self._transaction_lock = threading.RLock()

    def close(self):
        self.driver.close()

    def assert_writable(self, namespace):
        row = self.transaction(
            lambda tx: tx.run(
                "MATCH (s:MemorySpace {id:$ns}) RETURN s.replay_read_only AS readonly",
                ns=namespace,
            ).single()
        )
        if row and row["readonly"]:
            raise ValueError("Historical replay is read-only")

    def transaction(self, fn, *args):
        # Leader routing matters: a different MCP process must not read a lagging follower.
        with (
            self._transaction_lock,
            self.driver.session(
                database=self.database, default_access_mode=WRITE_ACCESS
            ) as session,
        ):
            return session.execute_write(fn, *args)

    def setup(self):
        self.driver.verify_connectivity()

        def run(tx):
            for label in (
                "MemorySpace",
                "MemoryEpisode",
                "MemoryEntity",
                "MemoryFact",
                "MemoryDream",
                "MemoryInsight",
                "MemoryRevision",
                "MemoryFeed",
                "MemoryInventory",
                "MemoryChange",
                "MemorySession",
                "MemoryMessage",
                "MemoryArtifact",
                "MemoryArtifactObservation",
            ):
                tx.run(
                    f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                ).consume()
            tx.run(
                "CREATE INDEX memory_entity_namespace IF NOT EXISTS FOR (n:MemoryEntity) ON (n.namespace)"
            ).consume()
            tx.run(
                "CREATE INDEX memory_episode_namespace IF NOT EXISTS FOR (n:MemoryEpisode) ON (n.namespace, n.status)"
            ).consume()
            tx.run(
                "CREATE INDEX memory_change_scope IF NOT EXISTS FOR (n:MemoryChange) ON (n.scope,n.sequence)"
            ).consume()
            # Staging links a tool result to its call and a message to its observations.
            tx.run(
                "CREATE INDEX memory_message_call IF NOT EXISTS FOR (n:MemoryMessage) ON (n.session_ref,n.call_id)"
            ).consume()
            tx.run(
                "CREATE INDEX memory_observation_message IF NOT EXISTS FOR (n:MemoryArtifactObservation) ON (n.message_ref)"
            ).consume()
            for field in ("subject_id", "target_id", "episode_id"):
                tx.run(
                    f"CREATE INDEX memory_fact_{field} IF NOT EXISTS FOR (n:MemoryFact) ON (n.namespace,n.{field})"
                ).consume()

        self.transaction(run)

    def mutate(self, tx, namespace, kind, details, operation, scoped=False):
        from .journal import Journal

        active = getattr(self._journal_local, "active", set())
        key = (id(tx), namespace)
        if key in active:
            return operation(tx)
        self._journal_local.active = active
        active.add(key)
        try:
            return Journal(self).mutate(tx, namespace, kind, details, operation, scoped)
        finally:
            active.remove(key)

    def journal_scopes(self):
        if not hasattr(self._journal_local, "scopes"):
            self._journal_local.scopes = {}
        return self._journal_local.scopes

    def touch(self, tx, namespace, label, ids):
        """Declare journaled nodes before writing them. Only a scoped journal
        write listens; under a full capture this is a no-op."""
        scope = self.journal_scopes().get((id(tx), namespace))
        if scope:
            scope.touch(label, ids)

    @staticmethod
    def lock(tx, namespace):
        # An explicit namespace write lock serializes canonicalization and revision checks.
        return tx.run(
            "MERGE (s:MemorySpace {id:$ns}) ON CREATE SET s.revision=0 "
            "SET s.lock=coalesce(s.lock,0)+1 RETURN s.revision AS revision",
            ns=namespace,
        ).single()["revision"]

    def stage(self, transcript: Transcript, *, transaction=None):
        payload = transcript.model_dump(mode="json")
        episode_id = digest(payload)
        timestamp = now().isoformat()

        def run(tx):
            self.lock(tx, transcript.namespace)
            self.touch(tx, transcript.namespace, "MemoryEpisode", [episode_id])
            record = tx.run(
                "MERGE (e:MemoryEpisode {id:$id}) ON CREATE SET e.namespace=$ns, "
                "e.source_id=$source, e.session_id=$session, e.payload=$payload, "
                "e.status='pending', e.ingested_at=$at, e.name=$name "
                "RETURN e.id AS episode_id,e.status AS status",
                id=episode_id,
                ns=transcript.namespace,
                source=transcript.source_id,
                session=transcript.session_id,
                payload=json.dumps(payload),
                name=(transcript.title or transcript.source_uri or "Episode").rsplit("/", 1)[-1][
                    :150
                ]
                + " · "
                + next(
                    (m.timestamp.isoformat() for m in transcript.messages if m.timestamp), "undated"
                ),
                at=timestamp,
            ).single()
            from .source_graph import save

            save(
                tx,
                transcript,
                episode_id,
                lambda label, ids: self.touch(tx, transcript.namespace, label, ids),
            )
            return dict(record)

        def operation(tx):
            return self.mutate(
                tx,
                transcript.namespace,
                "source_saved",
                {"episode_id": episode_id},
                run,
                scoped=True,
            )

        return operation(transaction) if transaction is not None else self.transaction(operation)

    def episode(self, namespace, episode_id):
        def run(tx):
            row = tx.run(
                "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) RETURN properties(e) AS e",
                id=episode_id,
                ns=namespace,
            ).single()
            if not row:
                raise ValueError("Episode not found in namespace")
            return row["e"]

        return self.transaction(run)

    def pending(self, namespace, limit=20):
        return self.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) WHERE e.status <> 'complete' "
                "RETURN e.id AS episode_id,e.source_id AS source_id,e.status AS status,e.error AS error "
                "ORDER BY e.ingested_at,e.id LIMIT $limit",
                ns=namespace,
                limit=limit,
            ).data()
        )

    def failed(self, namespace, episode_id, error, retry_feedback=None):
        self.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) WHERE e.status <> 'complete' "
                "SET e.status='failed',e.error=$error,"
                "e.retry_feedback=coalesce($feedback,e.retry_feedback)",
                id=episode_id,
                ns=namespace,
                error=error[:300],
                feedback=json.dumps(retry_feedback) if retry_feedback is not None else None,
            ).consume()
        )

    def cache_extraction(self, namespace, episode_id, extraction, model_info):
        """Persist validated model work independently of the canonical graph commit."""
        self.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) WHERE e.status <> 'complete' "
                "SET e.cached_extraction=$candidate,e.cached_engine=$engine,e.cached_model=$model",
                id=episode_id,
                ns=namespace,
                candidate=extraction.model_dump_json(),
                engine=self.engine,
                model=json.dumps(model_info),
            ).consume()
        )

    def retry_quarantined(self, namespace, episode_id):
        """Explicit operator retry; preserve candidate and rejection evidence."""
        self.assert_writable(namespace)
        row = self.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) "
                "SET e.worker_lock=coalesce(e.worker_lock,0)+1 "
                "WITH e WHERE e.status <> 'complete' AND e.quarantine_engine IS NOT NULL "
                "AND coalesce(e.lease_until,0)<=$now "
                "SET e.quarantine_engine=null,e.quarantine_reason=null,"
                "e.validation_failures=0,e.validation_engine=$engine,e.retry_after=0,e.attempts=0 "
                "RETURN e.id AS episode_id",
                id=episode_id,
                ns=namespace,
                now=now().timestamp(),
                engine=self.engine,
            ).single()
        )
        if not row:
            raise ValueError("No idle quarantined episode found in namespace")
        return {"episode_id": episode_id, "queued": True}

    @staticmethod
    def canonical(tx, namespace, entity, touch=None):
        names = list(
            dict.fromkeys(normalized(n) for n in [entity.key, entity.name, *entity.aliases])
        )
        match_names = [normalized(entity.key)] if entity.kind.value == "event" else names
        rows = tx.run(
            "MATCH (e:MemoryEntity {namespace:$ns,kind:$kind}) "
            "WHERE any(a IN $names WHERE a IN e.aliases) AND e.merged_into IS NULL "
            "RETURN properties(e) AS e",
            ns=namespace,
            kind=entity.kind.value,
            names=match_names,
        ).data()
        exact = [r for r in rows if normalized(r["e"]["key"]) == normalized(entity.key)]
        if exact:
            rows = exact
        elif entity.kind.value in ("person", "organization", "project", "habit"):
            # A common display name cannot collapse distinct scoped identities.
            # Existing keys are supplied to extractors; intentional aliases/merges
            # remain explicit. Different qualified owners/projects stay separate.
            aliases = {normalized(a) for a in entity.aliases}
            rows = [r for r in rows if normalized(r["e"]["key"]) in aliases]
        if len(rows) > 1:
            raise ValueError(
                f"Ambiguous identity for {entity.key}; merge or disambiguate explicitly"
            )
        key = rows[0]["e"]["key"] if rows else entity.key
        eid = rows[0]["e"]["id"] if rows else digest([namespace, entity.kind.value, key])
        if touch:
            touch("MemoryEntity", [eid])
        tx.run(
            "MERGE (e:MemoryEntity {id:$id}) ON CREATE SET e.namespace=$ns,e.kind=$kind,"
            "e.key=$key,e.name=$name,e.aliases=[] "
            "SET e.aliases=reduce(acc=e.aliases,a IN $names | CASE WHEN a IN acc THEN acc ELSE acc+a END)",
            id=eid,
            ns=namespace,
            kind=entity.kind.value,
            key=key,
            name=entity.name,
            names=names,
        ).consume()
        return eid, key

    def extraction_context(self, namespace, transcript):
        content = normalized("\n".join(m.content for m in transcript.messages))
        return self.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.merged_into IS NULL "
                "AND any(a IN e.aliases WHERE size(a)>=3 AND $content CONTAINS a) "
                "RETURN e.key AS key,e.kind AS kind,e.name AS name,e.aliases AS aliases ORDER BY e.key LIMIT 200",
                ns=namespace,
                content=content,
            ).data()
        )

    def relationship_context(self, namespace, entities):
        keys = [e["key"] for e in entities]
        return self.transaction(
            lambda tx: tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.subject IN $keys "
                "AND coalesce(f.retracted,false)=false AND f.slot IS NOT NULL "
                "RETURN DISTINCT f.subject AS subject,f.relation AS relation,f.slot AS slot,f.target AS target "
                "ORDER BY subject,relation,slot,target LIMIT 200",
                ns=namespace,
                keys=keys,
            ).data()
        )

    def commit(
        self,
        namespace: str,
        episode_id: str,
        extraction: Extraction,
        *,
        transaction=None,
        model_info=None,
    ):
        if engine_fingerprint() != self.engine:
            raise ValueError("Engine files changed during this process; restart before committing")
        # Validation is repeated against the durable episode inside the transaction.
        extraction_hash = digest(extraction.model_dump(mode="json"))
        committed_at = now().isoformat()

        def run(tx):
            self.lock(tx, namespace)

            def touch(label, ids):
                self.touch(tx, namespace, label, ids)

            touch("MemoryEpisode", [episode_id])
            row = tx.run(
                "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) RETURN properties(e) AS e",
                id=episode_id,
                ns=namespace,
            ).single()
            if not row:
                raise ValueError("Episode not found in namespace")
            episode = row["e"]
            if episode["status"] == "complete":
                if episode["extraction_hash"] != extraction_hash:
                    raise ValueError(
                        "Episode already committed with different extraction; retract incorrect facts explicitly"
                    )
                return {"episode_id": episode_id, "status": "complete", "replayed": True}
            transcript = Transcript.model_validate_json(episode["payload"])
            extraction.validate_evidence(transcript)
            identities = {
                e.key: self.canonical(tx, namespace, e, touch) for e in extraction.entities
            }
            touch(
                "MemoryFact",
                [digest([episode_id, f.model_dump(mode="json")]) for f in extraction.facts],
            )
            fact_ids = []
            for fact in extraction.facts:
                raw = fact.model_dump(mode="json")
                fid = digest([episode_id, raw])
                sid, skey = identities[fact.subject]
                tid, tkey = identities[fact.target]
                props = {
                    "namespace": namespace,
                    "subject": skey,
                    "target": tkey,
                    "subject_id": sid,
                    "target_id": tid,
                    "relation": fact.relation.value,
                    "status": fact.status,
                    "summary": fact.summary,
                    "name": fact.summary[:160],
                    "message_refs": [
                        digest([namespace, transcript.session_id, e.message_id])
                        for e in fact.evidence
                    ]
                    if transcript.source_format == "session-records-v1"
                    else [],
                    "slot": fact.slot,
                    "valid_at": fact.valid_at.isoformat() if fact.valid_at else None,
                    "valid_ts": fact.valid_at.timestamp() if fact.valid_at else None,
                    "time_basis": "explicit"
                    if fact.valid_at
                    else (
                        "document_updated"
                        if transcript.source_updated_at
                        else "document_created"
                        if transcript.source_created_at
                        else "unknown"
                    ),
                    "documented_at": (
                        transcript.source_updated_at or transcript.source_created_at
                    ).isoformat()
                    if (transcript.source_updated_at or transcript.source_created_at)
                    else None,
                    "documented_ts": (
                        transcript.source_updated_at or transcript.source_created_at
                    ).timestamp()
                    if (transcript.source_updated_at or transcript.source_created_at)
                    else None,
                    "confidence": fact.confidence,
                    "evidence": json.dumps(raw["evidence"]),
                    "validation_evidence": json.dumps(raw["validation_evidence"]),
                    "validation_message_refs": [
                        digest([namespace, transcript.session_id, e.message_id])
                        for e in fact.validation_evidence
                    ]
                    if transcript.source_format == "session-records-v1"
                    else [],
                    "episode_id": episode_id,
                    "session_id": transcript.session_id,
                    "source_kind": transcript.source_kind,
                    "recorded_at": committed_at,
                    "retracted": False,
                }
                tx.run(
                    "MATCH (e:MemoryEpisode {id:$ep}),(s:MemoryEntity {id:$sid}),(t:MemoryEntity {id:$tid}) "
                    "MERGE (f:MemoryFact {id:$id}) SET f += $props "
                    "MERGE (s)-[:HAS_FACT]->(f) MERGE (f)-[:TARGET]->(t) "
                    "MERGE (f)-[:SUPPORTED_BY]->(e)",
                    ep=episode_id,
                    sid=sid,
                    tid=tid,
                    id=fid,
                    props=props,
                ).consume()
                tx.run(
                    "MATCH (f:MemoryFact {id:$id}) UNWIND f.message_refs AS mid MATCH (m:MemoryMessage {id:mid,namespace:$ns}) MERGE (f)-[:CITES]->(m)",
                    id=fid,
                    ns=namespace,
                ).consume()
                tx.run(
                    "MATCH (f:MemoryFact {id:$id}) UNWIND f.validation_message_refs AS mid MATCH (m:MemoryMessage {id:mid,namespace:$ns}) MERGE (f)-[:VALIDATED_BY]->(m)",
                    id=fid,
                    ns=namespace,
                ).consume()
                fact_ids.append(fid)
            tx.run(
                "MATCH (e:MemoryEpisode {id:$id}) SET e.status='complete',e.error=null,e.retry_feedback=null,"
                "e.cached_extraction=null,e.cached_engine=null,e.cached_model=null,"
                "e.quarantine_engine=null,e.quarantine_reason=null,e.retry_after=null,"
                "e.lease_until=0,e.worker=null,"
                "e.extraction_hash=$hash,e.extraction_payload=$extraction,e.engine=$engine,e.model_info=$model_info,e.completed_at=$at,e.fact_count=$count "
                "WITH e MATCH (s:MemorySpace {id:$ns}) SET s.revision=s.revision+1",
                id=episode_id,
                hash=extraction_hash,
                extraction=extraction.model_dump_json(),
                engine=self.engine,
                model_info=json.dumps(model_info or {"provider": "caller"}),
                at=committed_at,
                count=len(fact_ids),
                ns=namespace,
            ).consume()
            return {
                "episode_id": episode_id,
                "status": "complete",
                "fact_ids": fact_ids,
                "entities": len(identities),
                "replayed": False,
            }

        def operation(tx):
            return self.mutate(
                tx,
                namespace,
                "facts_committed",
                {"episode_id": episode_id, "model": model_info or {"provider": "caller"}},
                run,
                scoped=True,
            )

        return operation(transaction) if transaction is not None else self.transaction(operation)

    @staticmethod
    def grounded_facts(tx, namespace, ids, relation=None):
        # Restore missing links only when all durable endpoint/evidence nodes exist.
        # Missing evidence is excluded, never silently represented as established fact.
        return tx.run(
            "MATCH (f:MemoryFact {namespace:$ns}) "
            "WHERE (f.subject_id IN $ids OR ($relation IS NULL AND f.target_id IN $ids)) "
            "AND ($relation IS NULL OR f.relation=$relation) "
            "MATCH (s:MemoryEntity {namespace:$ns}),(t:MemoryEntity {namespace:$ns}),"
            "(e:MemoryEpisode {namespace:$ns,status:'complete'}) "
            "WHERE s.id=f.subject_id AND t.id=f.target_id AND e.id=f.episode_id "
            "MERGE (s)-[:HAS_FACT]->(f) MERGE (f)-[:TARGET]->(t) "
            "MERGE (f)-[:SUPPORTED_BY]->(e) RETURN f {.*,subject_name:s.name,subject_kind:s.kind,target_name:t.name,target_kind:t.kind} AS fact",
            ns=namespace,
            ids=ids,
            relation=relation,
        ).data()

    def recall(
        self,
        namespace: str,
        query: str,
        as_of: datetime | None = None,
        limit=30,
        *,
        _complete=False,
        known_at=None,
        at_change=None,
    ):
        if known_at is not None or at_change is not None:
            from .journal import Journal

            return Journal(self).recall(
                namespace,
                query,
                as_of,
                limit,
                known_at=known_at,
                sequence=at_change,
                complete=_complete,
            )
        at = as_of or now()
        needle = normalized(query)

        def run(tx):
            revision = self.lock(tx, namespace)
            candidates = tx.run(
                "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.merged_into IS NULL "
                "AND (any(a IN e.aliases WHERE a CONTAINS $q) OR e.key=$q) "
                "RETURN properties(e) AS entity "
                "ORDER BY CASE WHEN $q IN e.aliases THEN 0 ELSE 1 END,e.key LIMIT 21",
                ns=namespace,
                q=needle,
            ).data()
            exact = [r for r in candidates if needle in r["entity"]["aliases"]]
            selected = exact or candidates
            ids = [r["entity"]["id"] for r in selected]
            # All root facts are resolved before response limits are applied.
            facts = self.grounded_facts(tx, namespace, ids)
            stored_count = tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.subject_id IN $ids OR f.target_id IN $ids "
                "RETURN count(f) AS count",
                ns=namespace,
                ids=ids,
            ).single()["count"]
            flat = [r["fact"] for r in facts]
            projection = project(flat, at)
            # Second hop follows only current framework facts, never stale neighbors.
            framework_ids = [
                f["target_id"] for f in projection["current"] if f["relation"] == "uses_framework"
            ]
            related = self.grounded_facts(tx, namespace, framework_ids, "implemented_in")
            second = project([r["fact"] for r in related], at)
            # One explicit, inspectable ontology rule; no guessed technologies.
            inferences = []
            for root in projection["current"]:
                for neighbor in second["current"]:
                    if (
                        root["relation"] == "uses_framework"
                        and root["target_id"] == neighbor["subject_id"]
                    ):
                        inferences.append(
                            {
                                "subject": root["subject"],
                                "relation": "uses_language",
                                "target": neighbor["target"],
                                "inferred": True,
                                "rule": "uses_framework + implemented_in",
                                "supporting_fact_ids": [root["id"], neighbor["id"]],
                            }
                        )
            status = tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN e.status AS status,count(*) AS count",
                ns=namespace,
            ).data()
            counts = {r["status"]: r["count"] for r in status}
            current_ids = {f["id"] for lane in ("current", "events") for f in projection[lane]}
            insights = tx.run(
                "MATCH (i:MemoryInsight {namespace:$ns}) WHERE coalesce(i.retired,false)=false AND any(k IN $ids WHERE k IN i.entity_ids) "
                "RETURN properties(i) AS insight",
                ns=namespace,
                ids=ids,
            ).data()
            insights = [
                r["insight"]
                for r in insights
                if all(fid in current_ids for fid in r["insight"]["supporting_fact_ids"])
            ]
            return {
                "query": query,
                "as_of": at.isoformat(),
                "revision": revision,
                "entities": [r["entity"] for r in selected[:20]],
                "ambiguous": len(selected) > 1,
                "entity_matches_truncated": len(selected) > 20,
                **{k: v if _complete else v[:limit] for k, v in projection.items()},
                "inferred": inferences[:limit],
                "insights": insights[:limit],
                "totals": {k: len(v) for k, v in projection.items()},
                "freshness": {
                    "complete_episodes": counts.get("complete", 0),
                    "pending_episodes": counts.get("pending", 0),
                    "failed_episodes": counts.get("failed", 0),
                    "excluded_ungrounded_facts": stored_count - len(facts),
                    "coverage": "latest committed evidence; unseen sessions are unknown",
                },
            }

        return self.transaction(run)

    def latest(
        self, namespace, entity, as_of=None, relation=None, *, known_at=None, at_change=None
    ):
        # Generic across entity kinds; resolve all evidence before applying output bounds.
        context = self.recall(
            namespace, entity, as_of, 100, _complete=True, known_at=known_at, at_change=at_change
        )
        entities = context["entities"]
        if len(entities) != 1:
            return {
                "status": "ambiguous" if entities else "not_found",
                "candidates": entities,
                "freshness": context["freshness"],
                **(
                    {"knowledge_history": context["knowledge_history"]}
                    if "knowledge_history" in context
                    else {}
                ),
            }

        def relevant(fact):
            return relation is None or fact["relation"] == relation

        eligible = [
            f
            for lane in ("current", "events", "history")
            for f in context[lane]
            if relevant(f) and f.get("valid_ts") is not None and f["status"] in ("active", "ended")
        ]
        newest = max((f["valid_ts"] for f in eligible), default=None)
        winners = {}
        for fact in eligible:
            if fact["valid_ts"] == newest:
                key = (
                    fact["subject"],
                    fact["relation"],
                    fact["target"],
                    fact.get("slot"),
                    fact["status"],
                )
                if key not in winners:
                    winners[key] = {**fact, "corroborating_fact_ids": []}
                winners[key]["corroborating_fact_ids"].extend(
                    fact.get("corroborating_fact_ids", [fact["id"]])
                )
        latest = sorted(winners.values(), key=lambda f: f["id"])
        conflicts = [
            f
            for f in context["conflicts"]
            if relevant(f) and (newest is None or f["valid_ts"] >= newest)
        ]
        unresolved = [
            f
            for lane in ("documented", "uncertain")
            for f in context[lane]
            if relevant(f)
            and f["status"] != "planned"
            and (f.get("valid_ts") is None or newest is None or f["valid_ts"] >= newest)
        ]
        status = (
            "conflict"
            if conflicts
            else "uncertain"
            if unresolved
            else "multiple"
            if len(latest) > 1
            else "found"
            if latest
            else "no_recorded_occurrence"
            if relation == "occurred"
            else "no_dated_evidence"
        )
        return {
            "status": status,
            "entity": entities[0],
            "relation": relation,
            "latest": latest[0] if len(latest) == 1 and not conflicts else None,
            "latest_facts": latest[:20],
            "latest_count": len(latest),
            "unresolved": unresolved[:20],
            "unresolved_count": len(unresolved),
            "conflicts": conflicts[:20],
            "certainty": "latest confirmed dated evidence only; unresolved claims may be newer"
            if unresolved
            else "conflicting latest claims"
            if conflicts
            else "latest committed dated evidence",
            "as_of": context["as_of"],
            "revision": context["revision"],
            "freshness": context["freshness"],
            **(
                {"knowledge_history": context["knowledge_history"]}
                if "knowledge_history" in context
                else {}
            ),
        }

    def retract(self, namespace, fact_id, reason):
        def run(tx):
            self.lock(tx, namespace)
            row = tx.run(
                "MATCH (f:MemoryFact {id:$id,namespace:$ns}) SET f.retracted=true,"
                "f.retraction_reason=$reason,f.retracted_at=$at RETURN f.id AS id",
                id=fact_id,
                ns=namespace,
                reason=reason,
                at=now().isoformat(),
            ).single()
            if not row:
                raise ValueError("Fact not found in namespace")
            tx.run(
                "MATCH (s:MemorySpace {id:$ns}) SET s.revision=s.revision+1", ns=namespace
            ).consume()
            return {"fact_id": fact_id, "retracted": True}

        return self.transaction(
            lambda tx: self.mutate(
                tx, namespace, "fact_retracted", {"fact_id": fact_id, "reason": reason}, run
            )
        )

    def merge(self, namespace, source_key, target_key, reason):
        if source_key == target_key:
            raise ValueError("Cannot merge an entity into itself")

        def run(tx):
            self.lock(tx, namespace)
            rows = tx.run(
                "MATCH (s:MemoryEntity {namespace:$ns,key:$source}),(t:MemoryEntity {namespace:$ns,key:$target}) "
                "WHERE s.merged_into IS NULL AND t.merged_into IS NULL RETURN properties(s) AS s,properties(t) AS t",
                ns=namespace,
                source=source_key,
                target=target_key,
            ).data()
            if len(rows) != 1 or rows[0]["s"]["kind"] != rows[0]["t"]["kind"]:
                raise ValueError("Merge requires two unambiguous entities of the same kind")
            s, t = rows[0]["s"], rows[0]["t"]
            tx.run(
                "MATCH (s:MemoryEntity {id:$s}),(t:MemoryEntity {id:$t}) "
                "SET s.merged_into=t.id,s.merge_reason=$reason "
                "SET t.aliases=reduce(acc=t.aliases,a IN s.aliases | CASE WHEN a IN acc THEN acc ELSE acc+a END)",
                s=s["id"],
                t=t["id"],
                reason=reason,
            ).consume()
            tx.run(
                "MATCH (s:MemoryEntity {id:$s})-[r:HAS_FACT]->(f),(t:MemoryEntity {id:$t}) "
                "MERGE (t)-[:HAS_FACT]->(f) SET f.subject_id=t.id,f.subject=t.key DELETE r",
                s=s["id"],
                t=t["id"],
            ).consume()
            tx.run(
                "MATCH (f)-[r:TARGET]->(s:MemoryEntity {id:$s}),(t:MemoryEntity {id:$t}) "
                "MERGE (f)-[:TARGET]->(t) SET f.target_id=t.id,f.target=t.key DELETE r",
                s=s["id"],
                t=t["id"],
            ).consume()
            tx.run(
                "MATCH (n:MemorySpace {id:$ns}) SET n.revision=n.revision+1", ns=namespace
            ).consume()
            return {"merged": source_key, "into": target_key}

        return self.transaction(
            lambda tx: self.mutate(
                tx,
                namespace,
                "entities_merged",
                {"source_key": source_key, "target_key": target_key, "reason": reason},
                run,
            )
        )

    def repair(self, namespace, transaction=None):
        def run(tx):
            self.lock(tx, namespace)
            from .source_graph import repair

            repair(tx, namespace)
            # Facts are the durable source of truth: restore missing structural edges.
            repaired = tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}),(s:MemoryEntity),(t:MemoryEntity),(e:MemoryEpisode) "
                "WHERE s.id=f.subject_id AND t.id=f.target_id AND e.id=f.episode_id "
                "MERGE (s)-[:HAS_FACT]->(f) MERGE (f)-[:TARGET]->(t) MERGE (f)-[:SUPPORTED_BY]->(e) "
                "RETURN count(f) AS checked",
                ns=namespace,
            ).single()["checked"]
            tx.run(
                "MATCH (i:MemoryInsight {namespace:$ns}) UNWIND i.supporting_fact_ids AS fid "
                "MATCH (f:MemoryFact {namespace:$ns,id:fid}) MERGE (i)-[:DERIVED_FROM]->(f)",
                ns=namespace,
            ).consume()
            orphaned = tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE NOT EXISTS { MATCH (:MemoryEntity)-[:HAS_FACT]->(f) } "
                "OR NOT EXISTS { MATCH (f)-[:TARGET]->(:MemoryEntity) } "
                "OR NOT EXISTS { MATCH (f)-[:SUPPORTED_BY]->(:MemoryEpisode) } "
                "RETURN count(f) AS count",
                ns=namespace,
            ).single()["count"]
            return {
                "checked_facts": repaired,
                "orphaned_facts": orphaned,
                "temporal_projection": "recomputed on every read",
            }

        return run(transaction) if transaction is not None else self.transaction(run)
