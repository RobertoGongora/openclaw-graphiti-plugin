import os
import uuid

import pytest

from graph_memory.store import GraphStore

# Every scoped journal write is cross-checked against a full capture of the graph.
os.environ.setdefault("MEMORY_JOURNAL_AUDIT", "1")


@pytest.fixture
def graph():
    uri = os.environ.get("MEMORY_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set MEMORY_TEST_NEO4J_URI to an isolated test Neo4j")
    store = GraphStore(uri, password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD"))
    store.setup()
    namespace = "eval:" + str(uuid.uuid4())
    yield store, namespace
    # Only the generated namespace; never erase the database or live graph.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
            ns=namespace,
        ).consume()
    )
    store.close()
