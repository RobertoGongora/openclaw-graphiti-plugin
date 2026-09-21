import io
import json
import logging

import pytest

from graph_memory import mcp
from graph_memory.mcp import BUSY, DENIED, LEGACY_VERSIONS, Protocol, host_allowed, stdio
from graph_memory.service import MemoryService

from .test_contracts import rpc


def protocol(**options):
    return Protocol(MemoryService(None), **options)


def run_stdio(monkeypatch, capsys, server, *lines):
    class Stdin:
        buffer = io.BytesIO(b"".join(lines))

    monkeypatch.setattr(mcp.sys, "stdin", Stdin)
    stdio(server)
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_stdio_survives_deep_nesting_bad_bytes_oversize_and_notifications(monkeypatch, capsys):
    ping = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    out = run_stdio(
        monkeypatch,
        capsys,
        protocol(),
        b"[" * 100_000 + b"\n",
        b"\xff\xfe\n",
        b"{not json\n",
        b"x" * (mcp.MAX_BODY + 10) + b"\n",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/unknown"}).encode() + b"\n",
        json.dumps(ping).encode() + b"\n",
    )
    assert [r.get("error", {}).get("code") for r in out] == [-32700, -32700, -32700, -32600, None]
    assert out[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


@pytest.mark.parametrize("requested", [*LEGACY_VERSIONS, "1999-01-01", None])
def test_legacy_initialize_echoes_supported_version_on_both_transports(requested):
    params = {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}
    if requested:
        params["protocolVersion"] = requested
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    expected = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[-1]
    for transport_headers in (None, {}, {"mcp-protocol-version": expected}):
        status, response = protocol().dispatch(message, transport_headers)
        assert status == 200
        assert response["result"]["protocolVersion"] == expected
        assert "resultType" not in response["result"]


def test_legacy_http_lists_tools_without_routing_headers_and_rejects_unknown_header_version():
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    status, response = protocol().dispatch(message, {"mcp-protocol-version": "2025-06-18"})
    assert status == 200 and response["result"]["tools"]
    status, response = protocol().dispatch(message, {"mcp-protocol-version": "1999-01-01"})
    assert status == 400 and response["error"]["code"] == -32022


def test_notifications_are_accepted_silently_and_invalid_requests_are_not():
    server = protocol()
    for method in ("notifications/initialized", "notifications/cancelled", "anything/else"):
        assert server.dispatch({"jsonrpc": "2.0", "method": method}, {}) == (202, None)
    assert server.dispatch([])[1]["error"]["code"] == -32600
    assert server.dispatch({"jsonrpc": "2.0", "id": True, "method": "ping"})[0] == 400
    assert server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "nope"})[1]["error"]["code"] == (
        -32601
    )
    bad_meta = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": []}}
    assert server.dispatch(bad_meta)[1]["error"]["code"] == -32602
    assert server.dispatch(rpc(cursor="x"))[1]["error"]["code"] == -32602
    no_caps = rpc()
    del no_caps["params"]["_meta"][mcp.PREFIX + "clientCapabilities"]
    assert server.dispatch(no_caps)[1]["error"]["code"] == -32602
    bad_info = rpc()
    bad_info["params"]["_meta"][mcp.PREFIX + "clientInfo"] = {"name": 1}
    assert server.dispatch(bad_info)[1]["error"]["code"] == -32602


def test_denials_use_server_defined_code():
    bound = protocol(namespace="personal", read_only=True)
    write = rpc("tools/call", name="memory_retract", arguments={"fact_id": "f", "reason": "r"})
    status, response = bound.dispatch(write)
    assert status == 403 and response["error"]["code"] == DENIED
    foreign = rpc("tools/call", name="memory_recall", arguments={"namespace": "x", "entity": "A"})
    status, response = bound.dispatch(foreign)
    assert status == 403 and response["error"]["code"] == DENIED
    assert -32099 <= DENIED <= -32000 and -32099 <= BUSY <= -32000


def test_tool_call_error_branches_log_type_and_id_but_never_content(caplog):
    server = protocol(namespace="personal")
    secret = "sk-very-private"

    def boom(_):
        raise RuntimeError(secret)

    def quoting(_):
        json.loads(secret)

    def engine(_):
        raise ValueError("Entity is ambiguous")

    schema = server.tools["memory_status"][0]
    call = rpc("tools/call", name="memory_status", arguments={})
    with caplog.at_level(logging.ERROR, logger="graph_memory.mcp"):
        for handler in (boom, quoting):
            server.tools["memory_status"] = (schema, handler, "")
            result = server.dispatch(call)[1]["result"]
            text = result["content"][0]["text"]
            assert result["isError"] and secret not in text
            request = text.rsplit(" ", 1)[-1]
            record = caplog.records[-1].getMessage()
            assert request in record and "memory_status" in record
            assert secret not in record
        assert "RuntimeError" in caplog.records[0].getMessage()
        assert "JSONDecodeError" in caplog.records[1].getMessage()
    server.tools["memory_status"] = (schema, engine, "")
    assert server.dispatch(call)[1]["result"]["content"][0]["text"] == "Entity is ambiguous"
    invalid = rpc("tools/call", name="memory_recall", arguments={"entity": 5, "junk": secret})
    result = server.dispatch(invalid)[1]["result"]
    assert result["isError"] and secret not in result["content"][0]["text"]
    not_object = rpc("tools/call", name="memory_status", arguments=[])
    assert server.dispatch(not_object)[1]["error"]["code"] == -32602


def test_dispatch_catch_all_returns_internal_error(caplog, monkeypatch):
    server = protocol()
    monkeypatch.setattr(server, "route", lambda *_: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="graph_memory.mcp"):
        status, response = server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert status == 500 and response["id"] == 3 and response["error"]["code"] == -32603
    assert "ZeroDivisionError" in caplog.text
    assert server.dispatch({"jsonrpc": "2.0", "method": "ping"}) == (202, None)


def test_catalog_is_cached_and_annotations_name_only_session_tools(monkeypatch):
    service = MemoryService(None)
    server = Protocol(service)
    monkeypatch.setattr(service, "session_tools", lambda: 1 / 0)
    tools = server.dispatch(rpc())[1]["result"]["tools"]
    assert server.dispatch(rpc())[1]["result"]["tools"] is tools
    hints = {t["name"]: t["annotations"] for t in tools}
    assert hints["memory_ingest"]["idempotentHint"] and hints["memory_ingest"]["openWorldHint"]
    assert not hints["memory_merge"]["idempotentHint"]
    assert [n for n, h in hints.items() if h["openWorldHint"]] == ["memory_ingest"]


def test_render_cypher_is_rejected_only_when_restricted_and_renders_are_bounded():
    server = protocol(namespace="personal")
    call = rpc("tools/call", name="memory_render", arguments={"cypher": "MATCH (n) RETURN n"})
    seen = []
    schema = server.tools["memory_render"][0]
    server.tools["memory_render"] = (schema, lambda r: seen.append(r.cypher) or {"nodes": 0}, "")
    assert server.dispatch(call)[0] == 200 and seen == ["MATCH (n) RETURN n"]
    server.renders.acquire()
    server.renders.acquire()
    status, response = server.dispatch(call)
    assert status == 503 and response["error"]["code"] == BUSY
    server.renders.release()
    assert server.dispatch(call)[0] == 200
    assert server.dispatch(call)[0] == 200  # The slot is returned after each render.
    server.renders.release()
    server.restrict_cypher()
    status, response = server.dispatch(call)
    assert status == 403 and response["error"]["code"] == DENIED and len(seen) == 3
    plain = rpc("tools/call", name="memory_render", arguments={})
    assert server.dispatch(plain)[0] == 200
    listed = {t["name"]: t["inputSchema"] for t in server.dispatch(rpc())[1]["result"]["tools"]}
    assert "cypher" not in listed["memory_render"]["properties"]
    assert "parameters" not in listed["memory_render"]["properties"]


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("127.0.0.1:8765", True),
        ("localhost:8766", True),
        ("LOCALHOST", True),
        ("[::1]:8765", True),
        ("attacker.test:8765", False),
        ("127.0.0.1.attacker.test", False),
        ("memory:8765", True),
        ("gateway.internal", True),
        ("gateway.internal:9", True),
        ("memory:9", False),
        ("", False),
        (None, False),
        ("a:b:c", False),
    ],
)
def test_host_allowlist(value, allowed):
    assert host_allowed(value, {"memory:8765", "gateway.internal"}) is allowed
