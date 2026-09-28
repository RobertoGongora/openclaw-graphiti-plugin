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
    # splitlines() also breaks on U+2028 and friends; records must survive it.
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
    # Only what the client received; the other side of a conflict is listed apart.
    assert record["result"]["fact_ids"] == ["fact-1", "fact-2"]
    assert record["result"]["facts"] == [["fact-1", "current"], ["fact-2", "history"]]
    assert record["result"]["conflict_fact_ids"] == ["fact-9"]
    assert record["transport"] == "stdio" and record["pid"] > 0
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


def test_call_log_scrubs_keys_case_and_environment_secrets(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    env_secret = "Neo4jPasswordValue77"
    monkeypatch.setenv("NEO4J_PASSWORD", env_secret)
    server = protocol(namespace="personal")
    # A result that echoes the question in lower case, as memory_search does.
    answering(
        server,
        "memory_render",
        {
            "status": "ok " + "x" * 220 + TOKEN,
            TOKEN: True,
            "question_terms": [TOKEN.lower()],
            "nodes": 1,
        },
    )
    render = rpc("tools/call", name="memory_render", arguments={"parameters": {TOKEN: 1}})
    status, _ = server.dispatch(render, {**headers(render), "authorization": f"Bearer {TOKEN}"})
    assert status == 200
    over_stdio = search(f"why is {env_secret.upper()} rejected", request_id=2)
    server.dispatch(over_stdio)
    body = path.read_text(encoding="utf-8").lower()
    assert TOKEN.lower() not in body and env_secret.lower() not in body
    # Truncation happens after scrubbing, so no prefix of the token survives either.
    assert TOKEN[:12].lower() not in body
    assert logged(path)[1]["arguments"]["question"] == "why is [redacted] rejected"


def test_call_log_ignores_client_chosen_secrets_and_the_public_default(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", "graph-memory")
    server = protocol(namespace="personal")
    message = search("what is in the graph-memory repo about personal notes")
    message["params"]["arguments"]["token"] = "personal"
    server.dispatch(message)
    [record] = logged(path)
    assert record["arguments"]["token"] == "[redacted]"
    assert (
        record["arguments"]["question"] == "what is in the graph-memory repo about personal notes"
    )
    assert record["arguments"]["namespace"] == "personal"


def test_call_log_bounds_every_line(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    huge = "k" * 1_000_000
    server.dispatch(rpc("tools/call", name=huge, arguments={}))
    server.dispatch(rpc("tools/call", name="memory_nope", arguments={huge: 1}))
    many = rpc(
        "tools/call", name="memory_nope", arguments={f"{i}{huge[:100_000]}": 1 for i in range(30)}
    )
    server.dispatch(many)
    long_id = rpc("tools/call", name="memory_status", arguments={})
    long_id["id"] = huge
    server.dispatch(long_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(len(line) <= call_log.LINE_LIMIT for line in lines)
    assert all(json.loads(line)["isError"] for line in lines)


def test_call_log_keeps_a_record_for_deeply_nested_arguments(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    answering(server, "memory_render", {"nodes": 0})
    nested = {}
    for _ in range(5000):
        nested = {"a": nested}
    message = rpc("tools/call", name="memory_render", arguments={"parameters": nested})
    assert server.dispatch(message)[0] == 200
    [record] = logged(path)
    assert record["tool"] == "memory_render" and record["ok"] is True


def test_call_log_escapes_line_separators(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    server.dispatch(search("x\u2028{}\u2029[1]\u0085null\u202ey"))
    [record] = logged(path)
    assert record["arguments"]["question"] == "x\u2028{}\u2029[1]\u0085null\u202ey"
    assert path.read_bytes().isascii()


@pytest.mark.parametrize("value", ["~/calls.jsonl", "calls.jsonl", "logs/calls.jsonl"])
def test_call_log_requires_an_absolute_path(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMORY_MCP_CALL_LOG", value)
    server = protocol(namespace="personal")
    server.dispatch(search("relative path question"))
    if value.startswith("~"):
        assert logged(tmp_path / "home" / "calls.jsonl")[0]["tool"] == "memory_search"
    else:
        assert not any(tmp_path.rglob("calls.jsonl"))
    assert not (tmp_path / "~").exists()


def test_call_log_refuses_links_fifos_and_loose_modes(monkeypatch, tmp_path, caplog):
    import os

    server = protocol(namespace="personal")
    victim = tmp_path / "victim"
    victim.write_text("VICTIM", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(victim)
    enable_log(monkeypatch, link)
    with caplog.at_level(logging.ERROR, logger="graph_memory.mcp"):
        assert server.dispatch(search("through a link"))[0] == 200
    assert victim.read_text(encoding="utf-8") == "VICTIM"
    assert "mcp call log failed" in caplog.text

    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    enable_log(monkeypatch, fifo)
    done = []
    worker = threading.Thread(target=lambda: done.append(server.dispatch(search("fifo"))))
    worker.start()
    worker.join(5)
    assert done and done[0][0] == 200

    loose = tmp_path / "loose.jsonl"
    loose.write_text("", encoding="utf-8")
    loose.chmod(0o644)
    enable_log(monkeypatch, loose)
    server.dispatch(search("loose mode"))
    assert loose.stat().st_mode & 0o777 == 0o600
    assert logged(loose)[0]["tool"] == "memory_search"


def test_call_log_never_grows_past_a_blocked_rotation(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    monkeypatch.setattr(call_log, "MAX_CALL_LOG_BYTES", 200)
    (tmp_path / "calls.jsonl.1").mkdir()
    (tmp_path / "calls.jsonl.1" / "keep").write_text("x", encoding="utf-8")
    server = protocol(namespace="personal")
    for i in range(20):
        assert server.dispatch(search(f"question {i}", request_id=i))[0] == 200
    assert path.stat().st_size < 200 + call_log.LINE_LIMIT


def test_call_log_repairs_a_torn_line(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_text('{"ts":"20', encoding="utf-8")
    path.chmod(0o600)
    enable_log(monkeypatch, path)
    protocol(namespace="personal").dispatch(search("after a torn write"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"ts":"20'
    assert json.loads(lines[1])["tool"] == "memory_search"


def test_call_log_rotation_is_safe_across_processes(tmp_path):
    import multiprocessing

    path = tmp_path / "calls.jsonl"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_append_many, args=(str(path), n, 400)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    rotated = tmp_path / "calls.jsonl.1"
    records = [json.loads(line) for f in (rotated, path) for line in f.read_text().splitlines()]
    # Each rotation moves a full file: the older generation is never a sliver.
    assert rotated.stat().st_size >= 20_000
    assert len(records) >= 20_000 // 120


def _append_many(path, worker, count):
    call_log.MAX_CALL_LOG_BYTES = 20_000
    for i in range(count):
        call_log.append_jsonl(path, json.dumps({"worker": worker, "i": i, "pad": "p" * 80}))


def test_call_log_recall_debug_shapes():
    full_recall = {
        "entity": None,
        "entities": [{"key": "project:atlas", "name": "Atlas", "kind": "project"}],
        "current": [{"id": "c1", "summary": "a"}, {"id": "c2", "summary": "b"}],
        "planned": [{"id": "p1", "summary": "c"}],
        "conflicts": [],
        "inferred": [{"summary": "i", "supporting_fact_ids": ["c1", "x9"]}],
        "totals": {"current": 2, "planned": 1, "conflicts": 4},
        "revision": 7,
    }
    compact = call_log.compact_output(full_recall)
    assert compact["fact_ids"] == ["c1", "c2", "p1"]
    assert compact["facts"][2] == ["p1", "planned"]
    assert compact["derived_support_ids"] == ["c1", "x9"]
    assert compact["totals"]["conflicts"] == 4
    assert compact["entities"][0]["key"] == "project:atlas"

    latest = {
        "status": "conflict",
        "entity": {"key": "project:atlas", "name": "Atlas", "kind": "project"},
        "facts": [{"id": "l1", "lane": "conflicts"}, {"id": "l2", "lane": "latest"}],
        "counts": {"latest": 1, "unresolved": 0, "conflicts_returned_by_engine": 1},
    }
    compact = call_log.compact_output(latest)
    assert compact["counts"] == {"latest": 1, "unresolved": 0, "conflicts_returned_by_engine": 1}
    assert compact["facts"] == [["l1", "conflicts"], ["l2", "latest"]]

    # Full detail: facts carry subject and target; a decision about another entity is related.
    related = {
        "entities": [{"key": "project:atlas", "name": "Atlas", "kind": "project"}],
        "facts": [
            {"id": "r0", "lane": "current", "subject": "project:atlas", "target": "db:pg"},
            {"id": "r1", "lane": "current", "subject": "project:ketch", "target": "db:pg"},
        ],
        "question_terms": [],
    }
    compact = call_log.compact_output(related)
    assert compact["facts"] == [["r0", "current"], ["r1", "current", "related"]]
    assert compact["question_term_count"] == 0

    many = {
        "status": "found",
        "facts": [{"id": f"{i:064x}", "lane": "current"} for i in range(100)],
        "counts": {"returned": 100},
    }
    compact = call_log.compact_output(many)
    assert compact["result_truncated"] is True and compact["counts"]["returned"] == 100
    assert len(compact["facts"]) > 20 and compact["facts_truncated"] == 100
    assert len(json.dumps(compact)) <= call_log.RESULT_JSON_LIMIT
    receipt = {"episode_id": "e", "entities": 9, "fact_ids": [f"{i:064x}" for i in range(120)]}
    compact = call_log.compact_output(receipt)
    assert compact["entities"] == 9 and compact["written_fact_ids"]

    searched = {
        "question": "calprivacy drop upload",
        "question_terms": ["calprivacy", "drop", "upload"],
        "status": "no_matching_facts",
        "facts": [
            {"id": "s1", "lane": "current", "subject": "project:drop", "target": "service:s3"}
        ],
    }
    compact = call_log.compact_output(searched)
    assert "question" not in compact and "question_terms" not in compact
    assert [row["key"] for row in compact["entities"]] == ["project:drop", "service:s3"]

    evidence = {
        "facts": [{"fact": {"id": "e1", "summary": "q"}}],
        "missing_fact_ids": ["e2"],
        "expand": {"tool": "memory_evidence", "arguments": {"fact_ids": ["e1", "e2"]}},
    }
    compact = call_log.compact_output(evidence)
    assert compact["fact_ids"] == ["e1"] and compact["missing_fact_ids"] == ["e2"]


def test_call_log_leaves_transcript_content_out_of_ingest(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    message = rpc(
        "tools/call",
        name="memory_ingest",
        arguments={
            "transcript": {
                "session_id": "s",
                "source_id": "src",
                "messages": [{"id": "m1", "role": "user", "content": PROSE * 20}],
            }
        },
    )
    server.dispatch(message)
    [record] = logged(path)
    assert PROSE not in path.read_text(encoding="utf-8")
    content = record["arguments"]["transcript"]["messages"][0]["content"]
    assert content == {"omitted": True, "chars": len(PROSE) * 20}


def test_call_log_scrub_is_linear_and_unicode_safe(monkeypatch, tmp_path):
    import time as clock

    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    secret = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", secret)
    server = protocol(namespace="personal")
    # lower() lengthens U+0130; an index-based scrub would leak or loop here.
    server.dispatch(search("\u0130" * 40 + " " + secret + " tail"))
    body = path.read_text(encoding="utf-8")
    assert secret not in body.lower() and "tail" in body
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", "TOKEN\u0130abcdefgh")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j:UriPassword123@db:7687")
    server.dispatch(search("x TOKEN\u0130abcdefgh and UriPassword123 y", request_id=5))
    assert logged(path)[-1]["arguments"]["question"] == "x [redacted] and [redacted] y"
    # A client-chosen header is not a secret: it cannot blank words or slow the scrub.
    message = search("İ" * 12 + " redacted " + "a" * 1_000_000, request_id=2)
    began = clock.perf_counter()
    server.dispatch(message, {**headers(message), "x-api-key": "aaaaaaaa"})
    assert clock.perf_counter() - began < 5
    assert "aaaaaaaa" in path.read_text(encoding="utf-8")


def test_call_log_scrubs_before_every_cut(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", TOKEN)
    server = protocol(namespace="personal")
    pad = "r" * 70
    answering(
        server,
        "memory_render",
        {
            "entities": [{"key": "k" * 110 + TOKEN, "name": "n", "kind": "project"}],
            "facts": [{"id": pad + TOKEN, "lane": "current"}],
            "conflict_fact_ids": [pad + TOKEN],
        },
    )
    render = rpc("tools/call", name="memory_render", arguments={})
    render["id"] = pad + TOKEN
    server.dispatch(render)
    unknown = search("q", request_id=3)
    unknown["params"]["arguments"][pad + TOKEN] = 1
    server.dispatch(unknown)
    body = path.read_text(encoding="utf-8").lower()
    assert TOKEN[:8].lower() not in body


def test_call_log_records_handler_time_on_failure(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    schema, _, description = server.tools["memory_status"]

    def boom(_request):
        raise RuntimeError("x")

    server.tools["memory_status"] = (schema, boom, description)
    server.dispatch(rpc("tools/call", name="memory_status", arguments={}))
    [record] = logged(path)
    assert record["handler_ms"] >= 0 and record["result"]["error_type"] == "RuntimeError"


def test_call_log_skips_a_directory_path_without_side_files(monkeypatch, tmp_path):
    target = tmp_path / "logs"
    target.mkdir()
    enable_log(monkeypatch, target)
    assert protocol(namespace="personal").dispatch(search("dir"))[0] == 200
    assert sorted(p.name for p in tmp_path.iterdir()) == ["logs"]


def test_call_log_pins_successful_live_reads_to_the_journal(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = protocol(namespace="personal")
    answering(server, "memory_search", {"status": "found", "as_of": "2026-09-01T00:00:00+00:00"})
    heads = iter([7, 7, 8, 9, 0, 0])
    monkeypatch.setattr(Protocol, "journal_head", lambda self, ns: next(heads))
    server.dispatch(search("stable"))
    server.dispatch(search("raced", request_id=2))
    server.dispatch(search("x" * 2000 + " " * 50, request_id=3))
    server.dispatch(search("pinned already", request_id=4, at_change=4))
    server.dispatch(search("", request_id=5))  # rejected: no replay, no head read
    stable, raced, padded, pinned, failed = logged(path)
    assert stable["journal_head"] == 7
    assert stable["replay"] == {
        "at_change": 7,
        "as_of": "2026-09-01T00:00:00+00:00",
        "raced": False,
        "intact": True,
    }
    assert raced["replay"]["raced"] is True
    assert padded["journal_head"] == 0 and padded["replay"]["intact"] is False
    assert "replay" not in pinned and "journal_head" not in pinned
    assert "replay" not in failed

    write = rpc("tools/call", name="memory_retract", arguments={"fact_id": "f", "reason": "r"})
    monkeypatch.setattr(Protocol, "journal_head", lambda self, ns: pytest.fail("write read"))
    server.dispatch(write)
    monkeypatch.delenv("MEMORY_MCP_CALL_LOG")
    monkeypatch.setattr(Protocol, "journal_head", lambda self, ns: pytest.fail("read while off"))
    server.dispatch(search("off"))


@pytest.mark.integration
def test_logged_live_reads_replay_after_later_writes(graph, monkeypatch, tmp_path):
    from .helpers import MYSQL, PG, PROJECT, ingest

    store, ns = graph
    fact = {"subject": PROJECT["key"], "relation": "uses_database", "slot": "primary"}
    ingest(
        store,
        ns,
        "s1",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [{**fact, "target": MYSQL["key"], "valid_at": "2026-09-01T00:00:00Z"}],
    )
    ingest(
        store,
        ns,
        "s2",
        "Atlas moved to Postgres.",
        [PROJECT, PG],
        [{**fact, "target": PG["key"], "valid_at": "2026-09-02T00:00:00Z"}],
    )
    path = tmp_path / "calls.jsonl"
    enable_log(monkeypatch, path)
    server = Protocol(MemoryService(store), ns)
    calls = [
        ("memory_search", {"question": "Atlas database", "include_history": True}),
        ("memory_recall", {"entity": "Atlas", "question": "database", "detail": "full"}),
        ("memory_latest", {"entity": "Atlas"}),
        ("memory_search_entities", {"query": "atlas"}),
    ]
    live = []
    for i, (name, arguments) in enumerate(calls):
        message = rpc("tools/call", name=name, arguments=arguments)
        message["id"] = i
        live.append(server.dispatch(message)[1]["result"]["structuredContent"])
    # A later write must not change what the replays return.
    ingest(
        store,
        ns,
        "s3",
        "Atlas uses MySQL again.",
        [PROJECT, MYSQL],
        [{**fact, "target": MYSQL["key"], "valid_at": "2026-09-03T00:00:00Z"}],
    )
    monkeypatch.delenv("MEMORY_MCP_CALL_LOG")
    for record, before in zip(logged(path), live, strict=True):
        replay = record["replay"]
        assert replay["raced"] is False and replay["intact"] is True
        message = rpc("tools/call", name=record["tool"])
        message["params"]["arguments"] = {
            **record["arguments"],
            **{k: v for k, v in replay.items() if k in ("at_change", "as_of")},
        }
        after = server.dispatch(message)[1]["result"]["structuredContent"]
        for volatile in ("knowledge_history", "freshness", "revision", "as_of"):
            before.pop(volatile, None), after.pop(volatile, None)
        assert after == before
