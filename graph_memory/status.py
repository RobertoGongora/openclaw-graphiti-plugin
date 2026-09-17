"""Bounded, namespace-scoped operational status without loading source payloads."""

import json
from datetime import datetime

from .inventory import inventory_id
from .models import now

# Processing is a lease, not an episode status. Match worker_tick's eligibility rules.
ACTIVE = "e.status <> 'complete' AND coalesce(e.lease_until,0)>$now"
QUEUED = (
    "e.status <> 'complete' AND coalesce(e.lease_until,0)<=$now AND coalesce(e.retry_after,0)<=$now"
)
DELAYED = (
    "e.status <> 'complete' AND coalesce(e.lease_until,0)<=$now AND coalesce(e.retry_after,0)>$now"
)
EPISODE = (
    "e {episode_id:e.id, .name, .source_id, .session_id, .status, .ingested_at, "
    ".completed_at, .fact_count, failed_attempts:coalesce(e.attempts,0), "
    ".lease_until, .retry_after}"
)


def status(store, request):
    def read(tx):
        checked = now()
        params = {"ns": request.namespace, "now": checked.timestamp()}
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
        return {
            "namespace": request.namespace,
            "checked_at": checked.isoformat(),
            "episodes": {"total": sum(counts.values()), "by_status": counts},
            "processing": {
                **processing,
                "is_processing": processing["active"] > 0,
                "active_episodes": active,
                "active_episodes_truncated": processing["active"] > len(active),
                "basis": "Unexpired episode leases; worker liveness is not monitored. Expired leases overlap queued/retry_delayed counts.",
            },
            "graph": graph,
            "source_inventory": inventory,
            "latest_episode": first("true", "e.ingested_at DESC"),
            "latest_completed_episode": first("e.status='complete'", "e.completed_at DESC"),
            "oldest_incomplete_episode": first("e.status <> 'complete'", "e.ingested_at"),
            "coverage": "Episode counts cover saved work. source_inventory separately estimates unstaged mounted transcripts at its scan time, when available; inspect its gaps and staleness. Scanner/worker liveness and unmounted sources are unknown. Counts may change during the read.",
        }

    return store.transaction(read)
