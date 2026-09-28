import io
import json
import logging
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from graph_memory import call_log, mcp
from graph_memory.admission import HistoryBusy
from graph_memory.mcp import (
    BUSY,
    DENIED,
    LEGACY_VERSIONS,
    Protocol,
    host_allowed,
    http_server,
    stdio,
)
from graph_memory.service import MemoryService

from .test_contracts import headers, rpc


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


TOKEN = "super-secret-token-value"
PROSE = "UNIQUEPROSE about Atlas disk growth"
PNG = "iVBORw0KGgo" + ("A" * 3000)


def enable_log(monkeypatch, path):
    monkeypatch.delenv("MEMORY_MCP_CALL_LOG", raising=False)
    monkeypatch.setenv("MEMORY_MCP_CALL_LOG", str(path))


def logged(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def answering(server, name, payload):
    schema, _, description = server.tools[name]
    server.tools[name] = (schema, lambda _request: payload, description)


def search(question, request_id=1, **extra):
    message = rpc("tools/call", name="memory_search", arguments={"question": question, **extra})
    message["id"] = request_id
    return message


def test_call_log_is_off_unless_a_path_is_set(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("MEMORY_MCP_CALL_LOG", raising=False)
    path = tmp_path / "calls.jsonl"
    server = protocol(namespace="personal")
    answering(
        server,
        "memory_search",
        {"status": "found", "facts": [{"id": "fact-1", "lane": "current", "text": PROSE}]},
    )
    question = "What did we decide about CalPrivacy uploads?"
    with caplog.at_level(logging.DEBUG, logger="graph_memory.mcp"):
        status, response = server.dispatch(search(question))
    assert status == 200 and response["result"]["structuredContent"]["facts"][0]["text"] == PROSE
    assert not path.exists()
    assert question not in caplog.text and PROSE not in caplog.text and TOKEN not in caplog.text


def test_call_log_records_bound_arguments_and_compact_results(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "calls.jsonl"
    enable_log(monkeypatch, path)

    def fail_audit(*_args, **_kwargs):
        raise AssertionError("call log must not audit the graph")

    monkeypatch.setattr("graph_memory.journal.audited", fail_audit)
    server = protocol(namespace="personal")
    question = "What did we decide about CalPrivacy uploads?"
    answering(
        server,
        "memory_search",
        {
            "status": "found",
            "question": question,
            "entities": [{"key": "calprivacy", "name": "CalPrivacy", "kind": "project"}],
            "facts": [
                {"id": "fact-1", "lane": "current", "text": PROSE},
                {"id": "fact-2", "lane": "history", "text": "older wording"},
            ],
            "counts": {"matching_unique": 2, "returned": 2, "stored_by_lane": {"current": 1}},
            "conflict_fact_ids": ["fact-9"],
            "evidence_tool": "memory_evidence",
            "guidance": "GUIDANCEPROSE " * 40,
        },
    )
    assert server.dispatch(rpc("ping"))[0] == 200
    assert server.dispatch(rpc())[0] == 200
    status, response = server.dispatch(search(question))
    assert status == 200 and PROSE in response["result"]["content"][0]["text"]
    assert logged(path)  # ping and tools/list are not calls
    [record] = logged(path)
    assert record["request_id"] == 1
    assert record["tool"] == "memory_search"
    assert record["ok"] is True and record["isError"] is False
    assert record["timing_ms"] >= 0
    assert record["arguments"]["question"] == question
    assert record["arguments"]["namespace"] == "personal"
    assert record["result"]["status"] == "found"
    assert record["result"]["counts"]["returned"] == 2
    assert record["result"]["counts"]["stored_by_lane"]["current"] == 1
    assert record["result"]["fact_ids"] == ["fact-1", "fact-2", "fact-9"]
    assert record["result"]["entities"][0]["key"] == "calprivacy"
    assert PROSE not in path.read_text(encoding="utf-8")
    assert "GUIDANCEPROSE" not in path.read_text(encoding="utf-8")
    assert "MemoryChange" not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600


def test_call_log_truncates_text_and_drops_image_bytes(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    huge = "q" * 4000
    message = search(huge)
    message["params"]["arguments"]["data"] = PNG
    status, response = server.dispatch(message)
    assert status == 200 and response["result"]["isError"] is True
    [record] = logged(path)
    assert record["isError"] is True
    assert record["arguments"]["question"].endswith("…[truncated]")
    assert len(record["arguments"]["question"]) == call_log.ARG_TEXT_LIMIT
    assert record["arguments"]["data"] == {"image_omitted": True, "chars": len(PNG)}
    body = path.read_text(encoding="utf-8")
    assert huge not in body and PNG not in body
    assert record["result"]["error"] == "validation"

    answering(
        server,
        "memory_render",
        {
            "nodes": 2,
            "relationships": 1,
            "width": 8,
            "height": 8,
            "status": "S" * 1000,
            "image": {"mimeType": "image/png", "data": PNG},
        },
    )
    message = rpc("tools/call", name="memory_render", arguments={})
    message["id"] = 2
    status, response = server.dispatch(message)
    assert status == 200 and response["result"]["content"][1]["data"] == PNG
    record = logged(path)[1]
    body = path.read_text(encoding="utf-8")
    assert record["result"]["image_omitted"] is True
    assert record["result"]["nodes"] == 2
    assert record["result"]["status"].endswith("…[truncated]")
    assert PNG not in body and "S" * 1000 not in body


def test_call_log_omits_credentials_and_rejected_payloads(monkeypatch, tmp_path, caplog):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    question = "What did we decide about CalPrivacy uploads?"
    leaked = f"{question} {TOKEN}"
    answering(
        server,
        "memory_search",
        {"status": "found", "facts": [{"id": "fact-1", "lane": "current", "text": "ok"}]},
    )
    message = search(leaked)
    message["params"]["arguments"]["token"] = TOKEN
    message["params"]["arguments"]["password"] = "hunter2-password"
    status, response = server.dispatch(
        message, {**headers(message), "authorization": f"Bearer {TOKEN}"}
    )
    # Extra credential fields fail schema validation; the call is still recorded.
    assert status == 200 and response["result"]["isError"] is True
    body = path.read_text(encoding="utf-8")
    assert TOKEN not in body and "hunter2-password" not in body
    assert "Bearer" not in body and "authorization" not in body.lower()
    assert question in body
    assert logged(path)[0]["arguments"]["token"] == "[redacted]"
    assert logged(path)[0]["arguments"]["password"] == "[redacted]"

    denied = protocol(namespace="personal", read_only=True)
    retract = rpc(
        "tools/call", name="memory_retract", arguments={"fact_id": "fact-1", "reason": "wrong"}
    )
    assert denied.dispatch(retract)[0] == 403
    assert logged(path)[1]["ok"] is False
    assert logged(path)[1]["arguments"]["namespace"] == "personal"
    assert logged(path)[1]["result"]["error_code"] == DENIED

    foreign = search("foreign scope question", request_id=3, namespace="other")
    assert server.dispatch(foreign)[0] == 403
    assert logged(path)[2]["result"]["error_code"] == DENIED
    assert logged(path)[2]["arguments"]["namespace"] == "other"

    unknown = rpc("tools/call", name="memory_nope", arguments={"question": "missing tool"})
    assert server.dispatch(unknown)[1]["error"]["code"] == -32602
    assert logged(path)[3]["tool"] == "memory_nope" and logged(path)[3]["isError"] is True

    invalid = rpc("tools/call", name="memory_status", arguments=["list-only-secret-value"])
    assert server.dispatch(invalid)[1]["error"]["code"] == -32602
    assert logged(path)[4]["arguments"] == {"_invalid": "arguments must be an object"}
    assert "list-only-secret-value" not in path.read_text(encoding="utf-8")

    busy = protocol(namespace="personal")
    schema, _, description = busy.tools["memory_status"]

    def raise_busy(_request):
        raise HistoryBusy()

    busy.tools["memory_status"] = (schema, raise_busy, description)
    assert busy.dispatch(rpc("tools/call", name="memory_status", arguments={}))[0] == 503
    assert logged(path)[5]["result"]["error_code"] == BUSY

    def boom(_request):
        raise RuntimeError(TOKEN)

    busy.tools["memory_status"] = (schema, boom, description)
    with caplog.at_level(logging.ERROR, logger="graph_memory.mcp"):
        failed = busy.dispatch(rpc("tools/call", name="memory_status", arguments={}))
    assert failed[1]["result"]["isError"] is True
    assert TOKEN not in failed[1]["result"]["content"][0]["text"]
    assert TOKEN not in path.read_text(encoding="utf-8")
    assert TOKEN not in caplog.text
    assert logged(path)[6]["result"]["error_type"] == "RuntimeError"
    assert logged(path)[6]["correlation_id"] in caplog.text


def test_call_log_rotates_and_survives_an_unwritable_path(monkeypatch, tmp_path, caplog):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    monkeypatch.setattr(call_log, "MAX_CALL_LOG_BYTES", 1)
    server = protocol(namespace="personal")
    answering(server, "memory_status", {"workers": 1})
    first = rpc("tools/call", name="memory_status", arguments={})
    second = rpc("tools/call", name="memory_status", arguments={})
    second["id"] = 2
    assert server.dispatch(first)[0] == 200
    assert server.dispatch(second)[0] == 200
    rotated = tmp_path / "calls.jsonl.1"
    assert logged(rotated)[0]["request_id"] == 1
    assert logged(path)[0]["request_id"] == 2
    assert path.stat().st_mode & 0o777 == 0o600

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MEMORY_MCP_CALL_LOG", str(blocker / "calls.jsonl"))
    with caplog.at_level(logging.ERROR, logger="graph_memory.mcp"):
        status, response = server.dispatch(first)
    assert status == 200 and response["result"]["isError"] is False
    assert "mcp call log failed" in caplog.text
    assert "memory_status" not in caplog.text


def test_call_log_follows_http_and_stdio(monkeypatch, tmp_path, capsys):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    token = "http-bearer-token-value"
    server = protocol(namespace="personal")
    answering(
        server,
        "memory_search",
        {"status": "found", "facts": [{"id": "fact-7", "lane": "current", "text": PROSE}]},
    )
    http = http_server(server, port=0, token=token)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}/mcp"
    message = search("What did we decide about CalPrivacy uploads?")

    def post(authorization):
        request_headers = {
            **headers(message),
            "Content-Type": "application/json",
            "Authorization": authorization,
        }
        try:
            with urlopen(
                Request(url, data=json.dumps(message).encode(), headers=request_headers)
            ) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        assert post("Bearer wrong-token")[0] == 401
        assert not path.exists()
        status, response = post(f"Bearer {token}")
        assert status == 200
        assert response["result"]["structuredContent"]["facts"][0]["id"] == "fact-7"
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
    body = path.read_text(encoding="utf-8")
    assert token not in body and "Bearer" not in body and "Authorization" not in body
    assert logged(path)[0]["tool"] == "memory_search"
    assert logged(path)[0]["result"]["fact_ids"] == ["fact-7"]
    assert PROSE not in body

    stdio_path = tmp_path / "stdio.jsonl"
    enable_log(monkeypatch, stdio_path)
    run_stdio(
        monkeypatch,
        capsys,
        server,
        json.dumps(search("stdio question", request_id=9)).encode() + b"\n",
    )
    assert logged(stdio_path)[0]["request_id"] == 9
    assert logged(stdio_path)[0]["arguments"]["question"] == "stdio question"
    assert logged(stdio_path)[0]["arguments"]["namespace"] == "personal"
