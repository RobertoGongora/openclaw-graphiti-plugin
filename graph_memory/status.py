"""Bounded, namespace-scoped operational status without loading source payloads."""

import json
from datetime import datetime

from .aliases import INDEXABLE
from .inventory import inventory_id
from .models import now

# Processing is a lease, not an episode status. Match worker_tick's eligibility rules.
ACTIVE = "e.status <> 'complete' AND coalesce(e.lease_until,0)>$now"
ELIGIBLE = "coalesce(e.quarantine_engine,'') <> $engine"
QUEUED = (
    (
        "e.status <> 'complete' AND coalesce(e.lease_until,0)<=$now AND coalesce(e.retry_after,0)<=$now"
    )
    + " AND "
    + ELIGIBLE
)
DELAYED = (
    (
        "e.status <> 'complete' AND coalesce(e.lease_until,0)<=$now AND coalesce(e.retry_after,0)>$now"
    )
    + " AND "
    + ELIGIBLE
)
EPISODE = (
    "e {episode_id:e.id, .name, .source_id, .session_id, .status, .ingested_at, "
    ".completed_at, .fact_count, failed_attempts:coalesce(e.attempts,0), "
    ".lease_until, .retry_after}"
)


CORROBORATING_SESSIONS = 3


def status(store, request):
    def read(tx):
        checked = now()
        params = {"ns": request.namespace, "now": checked.timestamp(), "engine": store.engine}
        rows = tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN e.status AS status, count(*) AS count",
            **params,
        ).data()
        counts = {"pending": 0, "complete": 0, "failed": 0}
        counts.update({row["status"] or "unknown": row["count"] for row in rows})
        processing = dict(
            tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN "
                f"sum(CASE WHEN {ACTIVE} THEN 1 ELSE 0 END) AS active, "
                f"sum(CASE WHEN {QUEUED} THEN 1 ELSE 0 END) AS queued, "
                f"sum(CASE WHEN {DELAYED} THEN 1 ELSE 0 END) AS retry_delayed, "
                "sum(CASE WHEN e.status <> 'complete' AND e.quarantine_engine=$engine THEN 1 ELSE 0 END) AS quarantined, "
                "sum(CASE WHEN e.status <> 'complete' AND e.cached_engine=$engine AND e.cached_extraction IS NOT NULL THEN 1 ELSE 0 END) AS cached_extractions, "
                "sum(CASE WHEN e.status <> 'complete' AND e.lease_until>0 "
                "AND e.lease_until<=$now THEN 1 ELSE 0 END) AS expired_leases",
                **params,
            ).single()
        )

        def episodes(where, order, limit=1):
            return [
                row["episode"]
                for row in tx.run(
                    "MATCH (e:MemoryEpisode {namespace:$ns}) "
                    f"WHERE {where} RETURN {EPISODE} AS episode "
                    f"ORDER BY {order}, e.id LIMIT $limit",
                    **params,
                    limit=limit,
                ).data()
            ]

        def first(where, order):
            rows = episodes(where, order)
            return rows[0] if rows else None

        graph = {}
        for label, key, predicate in (
            ("MemoryEntity", "entities", "n.merged_into IS NULL"),
            ("MemoryFact", "facts", "coalesce(n.retracted,false)=false"),
        ):
            graph[key] = tx.run(
                f"MATCH (n:{label} {{namespace:$ns}}) WHERE {predicate} RETURN count(*) AS count",
                ns=request.namespace,
            ).single()["count"]
        # Unverified claims made again in separate conversations. Repetition is a
        # reason to check a claim, never a promotion: uncertain stays uncertain
        # until a tool result validates it or a person confirms it.
        unconfirmed = (
            "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.status='uncertain' "
            "AND coalesce(f.retracted,false)=false AND f.confirmed_at IS NULL "
        )
        repeated = tx.run(
            unconfirmed + "WITH f ORDER BY f.recorded_at DESC, f.id "
            "WITH f.subject_id AS s,f.relation AS rel,f.target_id AS t,"
            "count(DISTINCT f.session_id) AS sessions,collect(f) AS fs "
            "WHERE sessions>=$min_sessions "
            "RETURN count(*) AS triples,sum(size(fs)) AS facts,"
            "collect({subject:fs[0].subject,relation:rel,target:fs[0].target,"
            "sessions:sessions,facts:size(fs),latest_fact_id:fs[0].id,"
            "text:substring(fs[0].summary,0,200)})[..$top] AS top",
            **params,
            min_sessions=CORROBORATING_SESSIONS,
            top=10,
        ).single()
        corroboration = {
            "min_sessions": CORROBORATING_SESSIONS,
            "uncertain_triples": repeated["triples"] if repeated else 0,
            "uncertain_facts": (repeated["facts"] if repeated else 0) or 0,
            "top": sorted(
                repeated["top"] if repeated else [],
                key=lambda x: (-x["sessions"], -x["facts"], x["latest_fact_id"]),
            ),
            "basis": "Unverified, unconfirmed claims stated in at least min_sessions separate sessions, "
            "one row per subject, relation and target with the latest wording. Check the claim, then "
            "memory_confirm the latest fact or let a tool result validate it; nothing is promoted here. "
            "Sessions can repeat recalled evidence; these counts do not establish independent support.",
        }
        active = episodes(ACTIVE, "e.ingested_at", 5)
        row = tx.run(
            "MATCH (i:MemoryInventory {id:$id, namespace:$ns}) RETURN i.payload AS payload",
            id=inventory_id(request.namespace),
            ns=request.namespace,
        ).single()
        inventory = {
            "state": "unavailable",
            "reason": "No transcript inventory scan has been saved.",
        }
        if row:
            inventory = json.loads(row["payload"])
            age = max(
                0, (checked - datetime.fromisoformat(inventory["finished_at"])).total_seconds()
            )
            duration = max(
                0,
                (
                    datetime.fromisoformat(inventory["finished_at"])
                    - datetime.fromisoformat(inventory["started_at"])
                ).total_seconds(),
            )
            inventory.update(
                age_seconds=round(age, 1),
                stale=age > max(600, 2 * inventory["refresh_interval_seconds"] + duration),
            )
        workers = [
            {
                "workers": w.get("workers"),
                "heartbeat_age_seconds": round(checked.timestamp() - w["heartbeat_at"]),
                "alive": checked.timestamp() - w["heartbeat_at"] < 180,
                "same_engine": w.get("engine") == store.engine,
                "provider_unavailable": w.get("provider_reason")
                if w.get("provider_open")
                else None,
            }
            for w in (
                row["w"]
                for row in tx.run(
                    "MATCH (w:MemoryWorker {namespace:$ns}) WHERE w.heartbeat_at IS NOT NULL "
                    "RETURN properties(w) AS w "
                    "ORDER BY w.heartbeat_at DESC LIMIT 5",
                    **params,
                )
            )
        ]
        names = tx.run(
            "MATCH (s:MemorySpace {id:$ns}) "
            "CALL { MATCH (a:MemoryAlias {namespace:$ns}) RETURN count(a) AS nodes } "
            "CALL { MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.merged_into IS NULL "
            # Only names short enough to have a lookup node, or the two never agree.
            "RETURN sum(size([a IN e.aliases WHERE size(a)<=$indexable])) AS listed } "
            "RETURN coalesce(s.aliases_indexed,false) AS indexed,nodes,listed",
            **params,
            indexable=INDEXABLE,
        ).single()
        return {
            "namespace": request.namespace,
            "checked_at": checked.isoformat(),
            "workers": workers,
            # The name lookup is derived from the alias lists. Fewer nodes than names
            # means something wrote entities without it (an older process): run
            # `aliases rebuild`.
            "entity_names": {
                "indexed": bool(names and names["indexed"]),
                "lookup_nodes": names["nodes"] if names else 0,
                "listed_names": (names["listed"] or 0) if names else 0,
            },
            "episodes": {"total": sum(counts.values()), "by_status": counts},
            "processing": {
                **processing,
                "is_processing": processing["active"] > 0,
                "active_episodes": active,
                "active_episodes_truncated": processing["active"] > len(active),
                "basis": "Unexpired episode leases; see workers for liveness. Expired leases overlap queued/retry_delayed counts.",
            },
            "graph": graph,
            "corroboration": corroboration,
            "source_inventory": inventory,
            "latest_episode": first("true", "e.ingested_at DESC"),
            "latest_completed_episode": first("e.status='complete'", "e.completed_at DESC"),
            "oldest_incomplete_episode": first("e.status <> 'complete'", "e.ingested_at"),
            "coverage": "Episode counts cover saved work. source_inventory separately estimates unstaged mounted transcripts at its scan time, when available; inspect its gaps and staleness. Worker liveness is its last heartbeat; unmounted sources are unknown. Counts may change during the read.",
        }

    return store.read(read)
