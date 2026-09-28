"""Opt-in local JSONL of MCP tools/call requests for recall debugging.

Unset MEMORY_MCP_CALL_LOG and nothing is written. A path is an explicit privacy
flip: questions and compact results stay on this machine. This is not graph
mutation audit and never writes change records.
"""

import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("graph_memory.mcp")

# One .1 generation, same cap as the OpenClaw debug log.
MAX_CALL_LOG_BYTES = 5 * 1024 * 1024
# memory_search questions are allowed up to 2000 characters; keep those whole.
ARG_TEXT_LIMIT = 2000
TEXT_LIMIT = 240
ARG_JSON_LIMIT = 8000
RESULT_JSON_LIMIT = 6000
ID_LIMIT = 50
_LOCK = threading.Lock()
_detail: ContextVar[dict | None] = ContextVar("mcp_call_detail", default=None)

_SECRET_KEYS = {
    "authorization",
    "authorisation",
    "proxy_authorization",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "bearer",
    "credential",
    "credentials",
    "neo4j_password",
    "memory_http_token",
    "memory_llm_api_key",
}
_BLOB_KEYS = {"data", "image", "png", "bytes", "base64", "blob"}
_HEAVY_KEYS = {
    "text",
    "summary",
    "quote",
    "guidance",
    "scope",
    "content",
    "transcript",
    "evidence",
    "basis",
    "description",
    "messages",
    "prompt",
    "html",
    "image",
    "data",
}
_STATUS_SKIP = _HEAVY_KEYS | {
    "facts",
    "derived",
    "conflicts",
    "unresolved",
    "latest",
    "latest_facts",
    "entities",
    "candidates",
    "top",
    "episodes",
    "active_episodes",
}
_FACT_LISTS = {
    "facts",
    "derived",
    "conflicts",
    "unresolved",
    "latest",
    "latest_facts",
    "items",
}
_ID_LIST_KEYS = {"conflict_fact_ids", "supporting_fact_ids", "fact_ids"}


def start_call(message):
    """Arm per-request detail when call logging is enabled. None leaves the request alone."""
    if not os.environ.get("MEMORY_MCP_CALL_LOG", "").strip():
        return None
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    if "id" not in message:
        return None
    return _detail.set({})


def note_call(**fields):
    detail = _detail.get()
    if detail is None:
        return
    try:
        _note(detail, fields)
    except Exception as exc:
        log.error("mcp call log note failed: %s", type(exc).__name__)


def _note(detail, fields):
    if "arguments" in fields:
        detail["arguments"] = _snapshot(fields.pop("arguments"))
    if "output" in fields:
        output = fields.pop("output")
        detail["output_secrets"] = _secret_values(output)
        detail["compact"] = compact_output(
            output, image_omitted=bool(fields.get("image_omitted"))
        )
    if "validation" in fields:
        detail["validation"] = _validation(fields.pop("validation"))
    detail.update(fields)


def finish_call(token, message, headers, status, response, started):
    if token is None:
        return
    try:
        path = os.environ.get("MEMORY_MCP_CALL_LOG", "").strip()
        if not path:
            return
        detail = _detail.get() or {}
        arguments = detail.get("arguments", _raw_arguments(message))
        secrets = (
            _secret_values(arguments)
            + list(detail.get("output_secrets") or [])
            + _header_secrets(headers)
        )
        record = _record(message, detail, arguments, status, response, started, secrets)
        append_jsonl(path, json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str))
    except Exception as exc:
        # Type only: the record holds the question the operator opted into storing.
        log.error("mcp call log failed: %s", type(exc).__name__)
    finally:
        _detail.reset(token)


def append_jsonl(path, line):
    """Append one line, rotating `<path>` to `<path>.1` once the live file reaches the cap."""
    data = line.encode("utf-8")
    if not data.endswith(b"\n"):
        data += b"\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        try:
            if target.exists() and target.stat().st_size >= MAX_CALL_LOG_BYTES:
                os.replace(target, target.with_name(target.name + ".1"))
        except OSError:
            pass
        created = not target.exists()
        fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if created:
                os.fchmod(fd, 0o600)
            view = data
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        finally:
            os.close(fd)


def compact_output(output, image_omitted=False):
    """Fact ids, entity keys, counts, and status. No image bytes or transcript text."""
    if not isinstance(output, dict):
        return {"preview": _truncate(str(output), TEXT_LIMIT)}
    result = {}
    if image_omitted or _contains_image(output):
        result["image_omitted"] = True
    status_fields = _status_fields(output)
    if status_fields:
        result.update(status_fields)
    ids = _fact_ids(output)
    if ids:
        result["fact_ids"] = ids[:ID_LIMIT]
        if len(ids) > ID_LIMIT:
            result["fact_ids_truncated"] = True
    entities = _entities(output)
    if entities:
        result["entities"] = entities[:20]
    return _cap_result(result)


def _record(message, detail, arguments, status, response, started, secrets):
    arguments, truncated = _bound_arguments(
        arguments,
        invalid=bool(detail.get("arguments_invalid")),
        secrets=secrets,
    )
    ok = _ok(status, response)
    result = dict(detail.get("compact") or {})
    if not ok:
        result.update(_failure(status, response, detail))
    elif "http_status" not in result:
        result["http_status"] = status
    result = _scrub(_cap_result(result), secrets)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "request_id": _request_id(message),
        "tool": detail.get("tool") if isinstance(detail.get("tool"), str) else _raw_tool(message),
        "arguments": arguments,
        "ok": ok,
        "isError": not ok,
        "timing_ms": elapsed,
        "result": result,
    }
    if detail.get("correlation_id"):
        record["correlation_id"] = detail["correlation_id"]
    if truncated:
        record["arguments_truncated"] = True
    return _scrub(record, secrets)


def _ok(status, response):
    if status != 200 or not isinstance(response, dict):
        return False
    result = response.get("result")
    return isinstance(result, dict) and not result.get("isError")


def _failure(status, response, detail):
    failure = {"http_status": status}
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        error = response["error"]
        failure["error_code"] = error.get("code")
        if isinstance(error.get("message"), str):
            failure["error"] = _truncate(error["message"], TEXT_LIMIT)
    elif detail.get("validation"):
        failure["error"] = "validation"
        failure["validation"] = detail["validation"]
    else:
        text = _error_text(response)
        if text and not detail.get("error_type"):
            failure["error"] = _truncate(text, TEXT_LIMIT)
    if detail.get("error_type"):
        failure["error_type"] = detail["error_type"]
    if detail.get("correlation_id"):
        failure["correlation_id"] = detail["correlation_id"]
    return failure


def _error_text(response):
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return None
    for block in result["content"]:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return None


def _bound_arguments(arguments, invalid, secrets):
    if invalid or not isinstance(arguments, dict):
        return {"_invalid": "arguments must be an object"}, False
    bounded = _walk(arguments, ARG_TEXT_LIMIT, secrets, drop_heavy=False)
    if len(json.dumps(bounded, default=str)) <= ARG_JSON_LIMIT:
        return bounded, False
    tighter = _walk(arguments, TEXT_LIMIT, secrets, drop_heavy=True)
    if len(json.dumps(tighter, default=str)) <= ARG_JSON_LIMIT:
        return tighter, True
    return {"_truncated": True, "keys": [str(k) for k in list(arguments)[:30]]}, True


def _walk(value, limit, secrets, drop_heavy, depth=0):
    if isinstance(value, str):
        return _truncate(_scrub_string(value, secrets), limit)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, list):
        if depth >= 6:
            return {"_truncated": True, "count": len(value)}
        items = [_walk(item, limit, secrets, drop_heavy, depth + 1) for item in value[:40]]
        if len(value) > 40:
            return {"items": items, "count": len(value), "truncated": True}
        return items
    if isinstance(value, dict):
        if _is_image(value):
            return {"image_omitted": True, "chars": _image_chars(value)}
        if depth >= 6:
            return {"_truncated": True}
        out = {}
        for key, item in value.items():
            name = str(key)
            if _is_secret_key(name):
                out[name] = "[redacted]"
                continue
            if _is_blob_key(name) or _is_image(item):
                out[name] = {"image_omitted": True, "chars": _image_chars(item)}
                continue
            if drop_heavy and name.lower() in _HEAVY_KEYS:
                out[name] = _omitted(item)
                continue
            out[name] = _walk(item, limit, secrets, drop_heavy, depth + 1)
        return out
    return _truncate(_scrub_string(str(value), secrets), limit)


def _status_fields(value, depth=0):
    if not isinstance(value, dict) or depth > 3:
        return {}
    out = {}
    for key, item in value.items():
        name = str(key)
        if _is_secret_key(name) or _is_blob_key(name) or name.lower() in _STATUS_SKIP or _is_image(item):
            continue
        if isinstance(item, bool) or item is None or isinstance(item, int):
            out[name] = item
        elif isinstance(item, float) and item == item and abs(item) != float("inf"):
            out[name] = item
        elif isinstance(item, str):
            out[name] = _truncate(item, TEXT_LIMIT)
        elif isinstance(item, dict):
            child = _status_fields(item, depth + 1)
            if child:
                out[name] = child
        elif isinstance(item, list) and item and all(
            isinstance(entry, (bool, int, float, str)) or entry is None for entry in item[:20]
        ):
            out[name] = [
                _truncate(entry, TEXT_LIMIT) if isinstance(entry, str) else entry for entry in item[:20]
            ]
            if len(item) > 20:
                out[name + "_truncated"] = True
    return out


def _fact_ids(value, key=None):
    found = []
    if isinstance(value, dict):
        if _is_image(value):
            return found
        for name, item in value.items():
            if name in _ID_LIST_KEYS and isinstance(item, list):
                found.extend(str(entry) for entry in item if isinstance(entry, (str, int)) and not isinstance(entry, bool))
            elif name in {"fact_id", "latest_fact_id"} and isinstance(item, (str, int)) and not isinstance(item, bool):
                found.append(str(item))
            elif name == "id" and isinstance(item, (str, int)) and not isinstance(item, bool) and _factish(value):
                found.append(str(item))
            elif name.lower() not in _BLOB_KEYS and not _is_image(item):
                found.extend(_fact_ids(item, name))
    elif isinstance(value, list) and (key in _FACT_LISTS or key in _ID_LIST_KEYS):
        for item in value:
            found.extend(_fact_ids(item, key))
    return list(dict.fromkeys(found))


def _factish(value):
    return any(name in value for name in ("lane", "relation", "text", "summary", "fact_id"))


def _entities(value):
    found = []
    seen = set()

    def visit(node):
        if isinstance(node, dict):
            if _is_image(node):
                return
            if isinstance(node.get("key"), str) and any(name in node for name in ("kind", "name")):
                key = node["key"]
                if key not in seen:
                    seen.add(key)
                    row = {"key": _truncate(key, 120)}
                    for name in ("name", "kind"):
                        if isinstance(node.get(name), str):
                            row[name] = _truncate(node[name], 120)
                    found.append(row)
            for name, item in node.items():
                if name.lower() not in _BLOB_KEYS:
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found


def _contains_image(value):
    if _is_image(value):
        return True
    if isinstance(value, dict):
        return any(_is_blob_key(str(key)) or _contains_image(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def _is_image(value):
    if not isinstance(value, dict):
        return False
    mime = value.get("mimeType") or value.get("mime_type")
    return (isinstance(mime, str) and mime.startswith("image/")) or value.get("type") == "image"


def _is_secret_key(key):
    normalized = str(key).lower().replace("-", "_")
    return (
        normalized in _SECRET_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
        or normalized.endswith("_api_key")
    )


def _is_blob_key(key):
    return str(key).lower().replace("-", "_") in _BLOB_KEYS


def _secret_values(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key) and isinstance(item, str):
                found.append(item)
                parts = item.strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    found.append(parts[1])
            else:
                _secret_values(item, found)
    elif isinstance(value, list):
        for item in value:
            _secret_values(item, found)
    return found


def _header_secrets(headers):
    secrets = []
    if not isinstance(headers, dict):
        return secrets
    for key, value in headers.items():
        if str(key).lower() not in {"authorization", "proxy-authorization", "x-api-key"}:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        token = value.strip()
        secrets.append(token)
        parts = token.split(None, 1)
        if len(parts) == 2:
            secrets.append(parts[1])
    return secrets


def _scrub(value, secrets):
    usable = [secret for secret in secrets if isinstance(secret, str) and len(secret) >= 8]
    usable.sort(key=len, reverse=True)
    return _scrub_value(value, usable)


def _scrub_value(value, secrets):
    if isinstance(value, str):
        return _scrub_string(value, secrets)
    if isinstance(value, list):
        return [_scrub_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item, secrets) for key, item in value.items()}
    return value


def _scrub_string(text, secrets):
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _validation(errors):
    rows = []
    if not isinstance(errors, list):
        return rows
    for item in errors[:20]:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        rows.append(
            {
                "loc": [_truncate(str(part), 80) for part in loc[:8]]
                if isinstance(loc, (list, tuple))
                else [],
                "msg": _truncate(str(item.get("msg", "")), TEXT_LIMIT),
            }
        )
    return rows


def _cap_result(result):
    if len(json.dumps(result, default=str)) <= RESULT_JSON_LIMIT:
        return result
    kept = {
        key: result[key]
        for key in (
            "http_status",
            "status",
            "ok",
            "fact_id",
            "fact_ids",
            "fact_ids_truncated",
            "counts",
            "image_omitted",
            "error",
            "error_code",
            "error_type",
            "correlation_id",
            "entity",
            "validation",
        )
        if key in result
    }
    kept["result_truncated"] = True
    return kept


def _snapshot(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"_unserializable": True}


def _raw_arguments(message):
    params = message.get("params") if isinstance(message, dict) else None
    if isinstance(params, dict) and "arguments" in params:
        return params.get("arguments")
    return {}


def _raw_tool(message):
    params = message.get("params") if isinstance(message, dict) else None
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        return params["name"]
    return None


def _request_id(message):
    if not isinstance(message, dict):
        return None
    request_id = message.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return None
    return request_id


def _truncate(text, limit):
    marker = "…[truncated]"
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker
    return text[: limit - len(marker)] + marker


def _omitted(value):
    if isinstance(value, str):
        return {"omitted": True, "chars": len(value)}
    if isinstance(value, list):
        return {"omitted": True, "count": len(value)}
    if isinstance(value, dict):
        return {"omitted": True, "keys": len(value)}
    return {"omitted": True}


def _image_chars(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        data = value.get("data")
        return len(data) if isinstance(data, str) else 0
    return 0
