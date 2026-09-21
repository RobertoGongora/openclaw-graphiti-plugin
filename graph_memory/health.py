"""Container health: is this role doing its job, asked without side effects."""

import json
import os
import socket
import time
from datetime import datetime
from urllib.request import Request, urlopen

from neo4j import GraphDatabase


def _query(cypher, **params):
    password = os.environ.get("NEO4J_PASSWORD")
    with GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), password) if password else None,
        connection_timeout=5,
        notifications_min_severity="OFF",
    ) as driver:
        records, _, _ = driver.execute_query(
            cypher, database_=os.environ.get("NEO4J_DATABASE", "neo4j"), routing_="r", **params
        )
        return [dict(r) for r in records]


def worker_id(namespace):
    return f"{namespace}:{socket.gethostname()}"


def check(role, namespace):
    now = time.time()
    if role == "worker":
        rows = _query(
            "MATCH (w:MemoryWorker {id:$id}) RETURN w.heartbeat_at AS at,w.interval AS interval",
            id=worker_id(namespace),
        )
        if not rows:
            return False, {"role": role, "reason": "no_heartbeat"}
        age = now - rows[0]["at"]
        # The scan loop beats once per interval; a long scan or drain may skip a few.
        return age < max(180, rows[0]["interval"] * 6), {"role": role, "heartbeat_age": round(age)}
    if role == "inventory":
        from .inventory import inventory_id

        rows = _query(
            "MATCH (i:MemoryInventory {id:$id}) RETURN properties(i) AS i",
            id=inventory_id(namespace),
        )
        if not rows:
            return False, {"role": role, "reason": "no_inventory"}
        snapshot = json.loads(rows[0]["i"]["payload"])
        finished = snapshot.get("finished_at")
        interval = float(snapshot.get("refresh_interval_seconds", 300))
        age = now - datetime.fromisoformat(finished).timestamp() if finished else None
        return (
            age is not None and age < interval * 3 + 120,
            {"role": role, "inventory_age": None if age is None else round(age)},
        )
    if role == "mcp":
        port = os.environ.get("MEMORY_HTTP_PORT", "8765")
        request = Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(
                    {"Authorization": "Bearer " + os.environ["MEMORY_HTTP_TOKEN"]}
                    if os.environ.get("MEMORY_HTTP_TOKEN")
                    else {}
                ),
            },
        )
        with urlopen(request, timeout=5) as response:
            return response.status == 200, {"role": role, "status": response.status}
    raise ValueError("role must be worker, inventory, or mcp")
