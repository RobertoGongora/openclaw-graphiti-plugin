import json
import threading
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from graph_memory.mcp import BUSY, DENIED, MAX_BODY, Protocol, http_server
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
                assert result["structuredContent"]["facts"][0]["text"] == "Atlas uses MySQL."
                assert "Mcp-Session-Id" not in response.headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def served():
    started = []

    def start(protocol=None, **options):
        server = http_server(
            protocol or Protocol(MemoryService(None), "private"), port=0, **options
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join()


def send(server, body, method="POST", path="/mcp", **extra):
    message = body if isinstance(body, dict) else None
    sent = {"Content-Type": "application/json", "Authorization": "Bearer test-token", **extra}
    if message and "_meta" in message.get("params", {}):
        sent = {**headers(message), **sent}
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        data = json.dumps(body).encode() if message is not None else body
        connection.request(method, path, body=data, headers={k: v for k, v in sent.items() if v})
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, response.headers
    finally:
        connection.close()


def test_http_hostile_input_gets_an_answer_not_a_dropped_connection(served):
    server = served(token="test-token")
    status, body, _ = send(server, b"[" * 100_000)
    assert status == 400 and body["error"]["code"] == -32700
    assert send(server, b"\xff")[1]["error"]["code"] == -32700
    assert send(server, rpc(), Authorization="Bearer té")[0] == 401
    assert send(server, rpc(), Authorization="")[0] == 401
    assert send(server, rpc())[0] == 200


def test_http_transport_errors_stay_plain_http(served):
    server = served(token="test-token")
    assert send(server, rpc(), path="/other")[0] == 404
    for method in ("GET", "DELETE", "PUT"):
        status, _, response_headers = send(server, None, method=method)
        assert status == 405 and response_headers["Allow"] == "POST"
    assert send(server, rpc(), **{"Content-Type": "text/plain"})[0] == 415
    assert send(server, b"")[0] == 413
    assert send(server, b"{}", **{"Content-Length": str(MAX_BODY + 1)})[0] == 413
    assert send(server, b"{}", **{"Transfer-Encoding": "chunked"})[0] == 413
    assert send(server, b"{}", **{"Content-Length": "many"})[0] == 400


def test_http_validates_host_and_configured_origins(served):
    server = served(
        token="test-token", origins=["http://localhost:8766"], hosts=["memory.internal:8765"]
    )
    assert send(server, rpc(), Host="attacker.test")[0] == 403
    assert send(server, rpc(), Host=f"attacker.test:{server.server_port}")[0] == 403
    for host in ("localhost:8766", "[::1]:1", "memory.internal:8765"):
        assert send(server, rpc(), Host=host)[0] == 200
    assert send(server, rpc(), Origin="http://localhost:8766")[0] == 200
    assert send(server, rpc(), Origin=f"http://127.0.0.1:{server.server_port}")[0] == 200
    assert send(server, rpc(), Origin="http://localhost:9999")[0] == 403


def test_http_legacy_client_initializes_without_a_session(served):
    server = served()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
    }
    status, body, response_headers = send(server, initialize)
    assert status == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    assert "Mcp-Session-Id" not in response_headers
    version = {"MCP-Protocol-Version": "2025-06-18"}
    status, body, _ = send(
        server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, **version
    )
    assert status == 202 and body is None
    assert send(server, {"jsonrpc": "2.0", "method": "notifications/other"})[0] == 202
    status, body, _ = send(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **version)
    assert status == 200 and body["result"]["tools"]


def test_http_token_binding_disables_render_cypher_and_busy_server_says_retry(served):
    call = rpc("tools/call", name="memory_render", arguments={"cypher": "MATCH (n) RETURN n"})
    server = served(token="test-token")
    status, body, _ = send(server, call)
    assert status == 403 and body["error"]["code"] == DENIED
    for _ in range(16):
        assert server.request_slots.acquire(blocking=False)
    try:
        status, body, response_headers = send(server, rpc())
        assert status == 503 and body["error"]["code"] == BUSY
        assert int(response_headers["Retry-After"]) > 0
    finally:
        for _ in range(16):
            server.request_slots.release()
    assert send(server, rpc())[0] == 200
