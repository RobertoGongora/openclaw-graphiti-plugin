import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from graph_memory.mcp import Protocol, http_server
from graph_memory.service import MemoryService

from .test_contracts import headers, rpc


def test_actual_http_stateless_origin_auth_and_header_mismatch():
    server = http_server(Protocol(MemoryService(None), "private"), port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/mcp"

    def post(message, extra=None):
        h = {
            **headers(message),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer test-token",
            **(extra or {}),
        }
        try:
            with urlopen(Request(url, data=json.dumps(message).encode(), headers=h)) as response:
                assert "Mcp-Session-Id" not in response.headers
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        assert post(rpc())[0] == 200
        assert post(rpc("server/discover"))[0] == 200
        assert post(rpc(), {"Origin": "https://attacker.test"})[0] == 403
        assert post(rpc(), {"Authorization": "Bearer wrong"})[0] == 401
        assert post(rpc(), {"Mcp-Method": "tools/call"})[1]["error"]["code"] == -32020
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_retrieves_committed_graph_without_initialization(graph):
    from .helpers import MYSQL, PROJECT, ingest

    store, ns = graph
    ingest(
        store,
        ns,
        "http",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "valid_at": "2026-09-15T10:00:00Z",
            }
        ],
    )
    server = http_server(Protocol(MemoryService(store), ns), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = rpc(
            "tools/call", name="memory_recall", arguments={"namespace": ns, "query": "Atlas"}
        )
        for _ in range(2):
            with urlopen(
                Request(
                    f"http://127.0.0.1:{server.server_port}/mcp",
                    data=json.dumps(request).encode(),
                    headers={**headers(request), "Content-Type": "application/json"},
                )
            ) as response:
                result = json.load(response)["result"]
                assert not result["isError"]
                assert result["structuredContent"]["current"][0]["target"] == MYSQL["key"]
                assert "Mcp-Session-Id" not in response.headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
