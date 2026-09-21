"""Typed application tools shared by HTTP, stdio, and CLI."""

import json
import time
import uuid

from . import models as m
from . import retrieval, status
from .diagnostics import SYSTEMIC, annotate, diagnostic
from .extraction_policy import extraction_payload
from .llm import DREAM_INSTRUCTIONS, ModelUnavailable, extraction_instructions
from .retry import feedback, restored_feedback
from .settings import lease_seconds
from .version import engine_fingerprint


class MemoryService:
    def __init__(self, store, llm=None):
        self.store, self.llm = store, llm

    def ingest(self, request: m.Ingest):
        receipt = self.store.stage(request.transcript)
        if receipt["status"] == "complete":
            return receipt
        if request.extract:
            return self.extract(
                m.EpisodeRequest(
                    namespace=request.transcript.namespace, episode_id=receipt["episode_id"]
                )
            )
        return {
            **receipt,
            "namespace": request.transcript.namespace,
            "available_for_recall": False,
            "processing": "queued_for_worker",
        }

    def remember(self, request: m.Remember):
        from .direct_ingest import prepare

        transcript = prepare(self.store, request)
        return self.ingest(m.Ingest(transcript=transcript))

    def prepare(self, request: m.EpisodeRequest):
        episode = self.store.episode(request.namespace, request.episode_id)
        transcript = m.Transcript.model_validate_json(episode["payload"])
        entities = self.store.extraction_context(request.namespace, transcript)
        return {
            "episode_id": request.episode_id,
            "status": episode["status"],
            "transcript": json.loads(episode["payload"]),
            "existing_entities": entities,
            "existing_relationships": self.store.relationship_context(request.namespace, entities),
            "instructions": extraction_instructions(transcript),
            "schema": m.Extraction.model_json_schema(),
            "next_tool": "memory_commit",
            "previous_rejection": restored_feedback(
                episode.get("retry_feedback"), self.store.engine
            ),
        }

    def extract(self, request: m.EpisodeRequest):
        self.store.assert_writable(request.namespace)
        started, stage, attempt = time.monotonic(), "prepare", -1
        extraction = None
        timings, mark = {}, [started]

        def lap(name):
            # Seconds per stage, summed over retries; lock waits count where they occur.
            current = time.monotonic()
            timings[name] = round(timings.get(name, 0) + current - mark[0], 3)
            mark[0] = current

        try:
            episode = self.store.episode(request.namespace, request.episode_id)
            if episode["status"] == "complete":
                return {"episode_id": request.episode_id, "status": "complete", "replayed": True}
            model_info = {
                "provider": type(self.llm).__name__,
                "model": getattr(self.llm, "model", None),
                "effort": getattr(self.llm, "effort", None),
            }
            if (
                episode.get("cached_engine") == self.store.engine
                and episode.get("cached_model") == json.dumps(model_info)
                and episode.get("cached_extraction")
            ):
                stage = "cached_validation"
                extraction = m.Extraction.model_validate_json(episode["cached_extraction"])
                extraction.validate_evidence(m.Transcript.model_validate_json(episode["payload"]))
                lap("prepare")
                stage = "commit"
                receipt = self.store.commit(
                    request.namespace, request.episode_id, extraction, model_info=model_info
                )
                lap("commit")
                return {**receipt, "timings": timings, "model_calls": 0, "cached": True}
            if not m.Transcript.model_validate_json(episode["payload"]).can_yield_facts():
                # Nothing in these messages can carry a fact; asking the model
                # would only spend a call to be told so.
                lap("prepare")
                stage = "commit"
                receipt = self.store.commit(
                    request.namespace,
                    request.episode_id,
                    m.Extraction(entities=[], facts=[]),
                    model_info={"provider": "none", "skipped": "no_claim_in_focus"},
                )
                lap("commit")
                return {
                    **receipt,
                    "timings": timings,
                    "model_calls": 0,
                    "cached": False,
                    "skipped": "no_claim_in_focus",
                }
            packet = self.prepare(request)
            if packet["status"] == "complete":
                return {"episode_id": request.episode_id, "status": "complete", "replayed": True}
            if self.llm is None:
                return {**packet, "status": "extraction_required"}
            payload = extraction_payload(
                {
                    "transcript": packet["transcript"],
                    "existing_entities": packet["existing_entities"],
                    "existing_relationships": packet["existing_relationships"],
                }
            )
            if packet.get("previous_rejection"):
                payload["previous_rejection"] = packet["previous_rejection"]
            lap("prepare")
            for attempt in range(2):
                stage = "model_output"
                extraction = None
                try:
                    extraction = self.llm.generate(packet["instructions"], payload, m.Extraction)
                finally:
                    lap("model")
                try:
                    stage = "evidence_validation"
                    extraction.validate_evidence(m.Transcript.model_validate(packet["transcript"]))
                    lap("validation")
                    break
                except ValueError as exc:
                    lap("validation")
                    if attempt:
                        raise
                    payload = {
                        **payload,
                        "rejected_candidate": extraction.model_dump(mode="json"),
                        "validation_error": str(exc),
                        "validation_diagnostic": diagnostic(exc, stage),
                        "correction": "Correct exact quote/focus/time grounding against the original transcript. Do not invent evidence or change source text.",
                    }
            stage = "checkpoint"
            self.store.cache_extraction(
                request.namespace, request.episode_id, extraction, model_info
            )
            lap("checkpoint")
            stage = "commit"
            receipt = self.store.commit(
                request.namespace,
                request.episode_id,
                extraction,
                model_info=model_info,
            )
            lap("commit")
            return {**receipt, "timings": timings, "model_calls": attempt + 1, "cached": False}
        except Exception as exc:
            issue = {
                **diagnostic(exc, stage),
                "timings": timings,
                "extraction_attempt": attempt + 1,
                "duration_seconds": round(time.monotonic() - started, 3),
                "engine": self.store.engine,
            }
            annotate(exc, diagnostic=issue)
            # A provider outage or a namespace fault leaves the episode as it was:
            # nothing about it failed.
            if isinstance(exc, ModelUnavailable) or issue["code"] in SYSTEMIC:
                raise
            self.store.failed(
                request.namespace,
                request.episode_id,
                type(exc).__name__,
                retry_feedback=feedback(
                    exc,
                    stage,
                    self.store.engine,
                    extraction.model_dump(mode="json") if extraction is not None else None,
                ),
            )
            raise

    def dream_create(self, request: m.DreamCreate):
        # Snapshot and source transcripts are captured in one serializable graph transaction.
        dream_id = str(uuid.uuid4())
        context = self.store.recall(request.namespace, request.query, limit=100)
        if any(n > 100 for n in context["totals"].values()) or context["entity_matches_truncated"]:
            raise ValueError(
                "Dream focus is too broad; narrow the query before creating a snapshot"
            )
        episodes = [self.store.episode(request.namespace, eid) for eid in request.episode_ids]
        if any(e["status"] != "complete" for e in episodes):
            raise ValueError("Dream inputs must be fully extracted episodes")
        snapshot = {
            "graph": context,
            "transcripts": [json.loads(e["payload"]) for e in episodes],
            "instructions": request.instructions,
        }

        def run(tx):
            revision = self.store.lock(tx, request.namespace)
            if revision != context["revision"]:
                raise ValueError("Graph changed while snapshotting; retry dream_create")
            tx.run(
                "CREATE (d:MemoryDream {id:$id,namespace:$ns,status:'pending',name:$name,snapshot:$snapshot,"
                "base_revision:$revision,engine:$engine,created_at:$at})",
                id=dream_id,
                ns=request.namespace,
                name="Dream · " + request.query[:130],
                snapshot=json.dumps(snapshot),
                revision=revision,
                engine=self.store.engine,
                at=m.now().isoformat(),
            ).consume()

        self.store.transaction(
            lambda tx: self.store.mutate(
                tx, request.namespace, "dream_created", {"dream_id": dream_id}, run
            )
        )
        return {"dream_id": dream_id, "status": "pending", "next_tool": "memory_dream_run"}

    def dream_get(self, request: m.DreamRequest):
        def run(tx):
            row = tx.run(
                "MATCH (d:MemoryDream {id:$id,namespace:$ns}) RETURN properties(d) AS d",
                id=request.dream_id,
                ns=request.namespace,
            ).single()
            if not row:
                raise ValueError("Dream not found in namespace")
            result = row["d"]
            for key in ("snapshot", "output"):
                if key in result:
                    result[key] = json.loads(result[key])
            return result

        return self.store.transaction(run)

    def dream_run(self, request: m.DreamRequest):
        self.store.assert_writable(request.namespace)
        if self.llm is None:
            raise ValueError("Configure MEMORY_LLM=codex for unattended dreaming")
        dream = self.dream_get(request)
        if dream.get("engine") != engine_fingerprint() or self.store.engine != engine_fingerprint(
            fresh=True
        ):
            raise ValueError("Dream engine changed; create a fresh dream")
        if dream["status"] in ("completed", "applied"):
            return dream
        # Renewable jobs: a killed worker can be retried after its bounded lease.
        token = str(uuid.uuid4())

        def claim(tx):
            self.store.lock(tx, request.namespace)
            row = tx.run(
                "MATCH (d:MemoryDream {id:$id,namespace:$ns}) "
                "WHERE d.status IN ['pending','failed'] OR (d.status='running' AND d.lease_until<$now) "
                "SET d.status='running',d.worker=$token,d.lease_until=$lease,d.error=null RETURN d.id AS id",
                id=request.dream_id,
                ns=request.namespace,
                token=token,
                now=m.now().timestamp(),
                lease=m.now().timestamp() + lease_seconds(self.llm),
            ).single()
            if not row:
                raise ValueError("Dream is already running or finished")

        self.store.transaction(claim)
        try:
            graph = dream["snapshot"]["graph"]
            facts = {f["id"]: f for lane in ("current", "events") for f in graph[lane]}
            payload = {**dream["snapshot"], "eligible_fact_ids": sorted(facts)}
            for attempt in range(2):
                output = self.llm.generate(DREAM_INSTRUCTIONS, payload, m.DreamOutput)
                try:
                    for insight in output.insights:
                        if not set(insight.supporting_fact_ids) <= facts.keys():
                            raise ValueError(
                                "Dream cites facts outside its current evidence snapshot"
                            )
                        keys = {
                            facts[fid][side]
                            for fid in insight.supporting_fact_ids
                            for side in ("subject", "target")
                        }
                        if not set(insight.entity_keys) <= keys:
                            raise ValueError(
                                "Dream insight entities must occur in its supporting facts"
                            )
                    break
                except ValueError as exc:
                    if attempt:
                        raise
                    payload = {
                        **payload,
                        "rejected_candidate": output.model_dump(mode="json"),
                        "validation_error": str(exc),
                        "correction": "Correct grounding using only eligible_fact_ids. Move unsupported conclusions to observations; never invent support.",
                    }

            def complete(tx):
                row = tx.run(
                    "MATCH (d:MemoryDream {id:$id,namespace:$ns,worker:$token,status:'running'}) "
                    "SET d.status='completed',d.output=$output,d.completed_at=$at RETURN d.id AS id",
                    id=request.dream_id,
                    ns=request.namespace,
                    token=token,
                    output=output.model_dump_json(),
                    at=m.now().isoformat(),
                ).single()
                if not row:
                    raise ValueError("Dream worker lease was replaced")

            self.store.transaction(
                lambda tx: self.store.mutate(
                    tx,
                    request.namespace,
                    "dream_completed",
                    {"dream_id": request.dream_id},
                    complete,
                )
            )
        except Exception as exc:
            error_type = type(exc).__name__
            self.store.transaction(
                lambda tx: tx.run(
                    "MATCH (d:MemoryDream {id:$id,namespace:$ns,worker:$token}) SET d.status='failed',d.error=$error",
                    id=request.dream_id,
                    ns=request.namespace,
                    token=token,
                    error=error_type,
                ).consume()
            )
            raise
        return self.dream_get(request)

    def dream_apply(self, request: m.DreamRequest):
        def run(tx):
            revision = self.store.lock(tx, request.namespace)
            row = tx.run(
                "MATCH (d:MemoryDream {id:$id,namespace:$ns}) RETURN properties(d) AS d",
                id=request.dream_id,
                ns=request.namespace,
            ).single()
            if not row:
                raise ValueError("Dream not found")
            dream = row["d"]
            if dream.get("engine") != engine_fingerprint():
                raise ValueError("Dream engine changed; create a fresh dream")
            if dream["status"] == "applied":
                return {"dream_id": request.dream_id, "status": "applied", "replayed": True}
            if dream["status"] != "completed":
                raise ValueError("Only completed dreams can be applied")
            if dream["base_revision"] != revision:
                raise ValueError("Dream is stale: graph changed; create a new dream")
            output = m.DreamOutput.model_validate_json(dream["output"])
            for index, insight in enumerate(output.insights):
                entities = tx.run(
                    "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.key IN $keys AND e.merged_into IS NULL "
                    "RETURN e.id AS id",
                    ns=request.namespace,
                    keys=insight.entity_keys,
                ).data()
                tx.run(
                    "CREATE (i:MemoryInsight {id:$id,namespace:$ns,summary:$summary,name:$summary,entity_ids:$entities,"
                    "supporting_fact_ids:$facts,confidence:$confidence,inferred:true,dream_id:$dream}) "
                    "WITH i UNWIND $facts AS fid MATCH (f:MemoryFact {id:fid}) MERGE (i)-[:DERIVED_FROM]->(f)",
                    id=f"{request.dream_id}:{index}",
                    ns=request.namespace,
                    summary=insight.summary,
                    entities=[e["id"] for e in entities],
                    facts=insight.supporting_fact_ids,
                    confidence=insight.confidence,
                    dream=request.dream_id,
                ).consume()
            tx.run(
                "MATCH (d:MemoryDream {id:$id}) SET d.status='applied' "
                "WITH d MATCH (s:MemorySpace {id:$ns}) SET s.revision=s.revision+1",
                id=request.dream_id,
                ns=request.namespace,
            ).consume()
            return {
                "dream_id": request.dream_id,
                "status": "applied",
                "insights": len(output.insights),
            }

        return self.store.transaction(
            lambda tx: self.store.mutate(
                tx, request.namespace, "insights_published", {"dream_id": request.dream_id}, run
            )
        )

    def tools(self):
        # The tuple owns both validation and dispatch, preventing schema/handler drift.
        from .render import render_graph

        return {
            "memory_render": (
                m.Render,
                lambda r: render_graph(self.store, r),
                "Use when the user wants to see their memory graph or how its facts connect. Shows the whole graph by default, or a selected view using optional Cypher.",
            ),
            "memory_ingest": (
                m.Ingest,
                self.ingest,
                "Use when the user asks you to remember something or when saving new information from a conversation.",
            ),
            "memory_prepare": (
                m.EpisodeRequest,
                self.prepare,
                "Get durable transcript, extraction instructions and Pydantic JSON Schema for any calling LLM.",
            ),
            "memory_extract": (
                m.EpisodeRequest,
                self.extract,
                "Extract a pending episode with the configured LLM, or return caller extraction instructions.",
            ),
            "memory_commit": (
                m.Commit,
                lambda r: self.store.commit(r.namespace, r.episode_id, r.extraction),
                "Validate typed relationships and exact source quotes, then atomically commit graph facts. Idempotent.",
            ),
            "memory_search_entities": (
                retrieval.EntitySearch,
                lambda r: retrieval.search_entities(self.store, r),
                "Use when you need to find a remembered person, project, or thing and are unsure of its name or identity.",
            ),
            "memory_evidence": (
                retrieval.EvidenceRequest,
                lambda r: retrieval.evidence(self.store, r),
                "Use when you need to verify a recalled fact or inspect the evidence behind it.",
            ),
            "memory_recall": (
                m.Recall,
                lambda r: self.store.recall(
                    r.namespace,
                    r.query,
                    r.as_of,
                    r.limit,
                    known_at=r.known_at,
                    at_change=r.at_change,
                ),
                "Use when the user asks about their projects, preferences, people, decisions, or past work.",
            ),
            "memory_latest": (
                m.Latest,
                lambda r: self.store.latest(
                    r.namespace,
                    r.entity,
                    r.as_of,
                    r.relation,
                    known_at=r.known_at,
                    at_change=r.at_change,
                ),
                "Use when the user asks when something last happened or what was most recently recorded about a subject.",
            ),
            "memory_status": (
                m.Scope,
                lambda r: status.status(self.store, r),
                "Use when you want to check memory ingestion progress, how much remains unstaged, processing or failed work, and graph counts.",
            ),
            "memory_pending": (
                m.Pending,
                lambda r: {"episodes": self.store.pending(r.namespace, r.limit)},
                "List incomplete/failed extraction receipts for retry; they are not current graph knowledge.",
            ),
            "memory_retract": (
                m.Retract,
                lambda r: self.store.retract(r.namespace, r.fact_id, r.reason),
                "Use when the user says a remembered fact is incorrect or should no longer inform answers.",
            ),
            "memory_confirm": (
                m.Confirm,
                lambda r: self.store.confirm(r.namespace, r.fact_id, r.note, r.valid_at),
                "Use when the user states that an uncertain remembered fact is true. Never confirm on your own judgement.",
            ),
            "memory_merge": (
                m.Merge,
                lambda r: self.store.merge(r.namespace, r.source_key, r.target_key, r.reason),
                "Use when separate memory entries are confirmed to refer to the same person, project, or thing.",
            ),
            "memory_repair": (
                m.Scope,
                lambda r: self.store.repair(r.namespace),
                "Restore missing graph links from durable facts and report orphaned evidence. Never fabricate missing content.",
            ),
            "memory_dream_create": (
                m.DreamCreate,
                self.dream_create,
                "Snapshot graph and 1-100 extracted transcripts for a separate consolidation dream. Original inputs remain unchanged.",
            ),
            "memory_dream_run": (
                m.DreamRequest,
                self.dream_run,
                "Run or retry the durable dream using the configured model. May take minutes; CLI worker recommended.",
            ),
            "memory_dream_get": (
                m.DreamRequest,
                self.dream_get,
                "Read a dream status, immutable snapshot and candidate output.",
            ),
            "memory_dream_apply": (
                m.DreamRequest,
                self.dream_apply,
                "Promote completed dream insights only if graph revision still matches; facts stay unchanged.",
            ),
        }

    def session_tools(self):
        """The public MCP surface; orchestration remains in the engine and CLI."""
        names = {
            "memory_status",
            "memory_render",
            "memory_evidence",
            "memory_search_entities",
            "memory_ingest",
            "memory_recall",
            "memory_latest",
            "memory_retract",
            "memory_confirm",
            "memory_merge",
        }
        catalog = {name: entry for name, entry in self.tools().items() if name in names}
        catalog["memory_ingest"] = (
            m.Remember,
            self.remember,
            catalog["memory_ingest"][2],
        )
        for name, schema, handler in (
            ("memory_recall", retrieval.RecallView, retrieval.recall),
            ("memory_latest", retrieval.LatestView, retrieval.latest),
        ):
            catalog[name] = (
                schema,
                lambda r, handler=handler: handler(self.store, r),
                catalog[name][2],
            )
        return catalog

    def call(self, name, arguments):
        if name not in self.tools():
            raise KeyError(name)
        schema, handler, _ = self.tools()[name]
        return handler(schema.model_validate(arguments))
