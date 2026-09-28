"""Opt-in local JSONL of MCP tools/call requests for recall debugging.

Unset MEMORY_MCP_CALL_LOG and nothing is written. A path is an explicit privacy
flip: questions and compact results stay on this machine. This is not graph
mutation audit and never writes change records.
"""

import errno
import fcntl
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .credentials import DEFAULT_PASSWORD

log = logging.getLogger("graph_memory.mcp")

ENV = "MEMORY_MCP_CALL_LOG"
# One .1 generation, same cap as the OpenClaw debug log.
MAX_CALL_LOG_BYTES = 5 * 1024 * 1024
# memory_search questions are allowed up to 2000 characters; keep those whole.
ARG_TEXT_LIMIT = 2000
TEXT_LIMIT = 240
KEY_LIMIT = 80
ARG_JSON_LIMIT = 8000
RESULT_JSON_LIMIT = 7000
# Whole line, after everything else; a record past it keeps only who, when and how.
LINE_LIMIT = 16_000
# memory_recall returns up to 100 facts.
ID_LIMIT = 100
MAX_DEPTH = 6
MARKER = "…[truncated]"
# Server credentials a question or a result could quote back.
SECRET_ENV = ("MEMORY_HTTP_TOKEN", "NEO4J_PASSWORD", "MEMORY_LLM_API_KEY", "TRANSCRIPT_MCP_TOKEN")
_LOCK = threading.Lock()
_detail: ContextVar[dict | None] = ContextVar("mcp_call_detail", default=None)
# Compiled credential pattern for this request; every _truncate scrubs with it first.
_pattern: ContextVar[re.Pattern | None] = ContextVar("mcp_call_secrets", default=None)

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
# Argument fields dropped when the whole argument object is over budget.
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
# Message content and evidence quotes: transcript text, never needed to debug recall.
_ARG_PROSE = {"content", "quote"}
# Result fields that are prose or that repeat the question (already in arguments,
# and a tokenized copy would slip past the credential scrub).
_RESULT_PROSE = _HEAVY_KEYS | {"question", "question_terms", "query", "certainty", "coverage"}
# Lists of facts in any tool result; the rank order within each list is kept.
_FACT_LISTS = (
    "facts",
    "latest_facts",
    "current",
    "planned",
    "events",
    "uncertain",
    "documented",
    "history",
    "conflicts",
    "unresolved",
)
_DERIVED_LISTS = ("derived", "inferred", "insights")
# Structural keys walked separately; skipped when copying status fields.
_STRUCTURE = {
    *_FACT_LISTS,
    *_DERIVED_LISTS,
    "latest",
    "entity",
    "candidates",
    "matches",
    "top",
    "active_episodes",
    "fact_ids",
    "conflict_fact_ids",
    "missing_fact_ids",
    "supporting_fact_ids",
    "related_fact_ids",
    "expand",
    "suggested_call",
}


def enabled_path():
    """The configured log path, or None. `~` is expanded; a relative path is refused."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else None


def announce():
    """One stderr line at serve startup so an enabled privacy flip is never silent."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return
    path = enabled_path()
    if path is None:
        print(f"graph-memory: {ENV} must be an absolute path; call log is off", file=sys.stderr)
    else:
        print(f"graph-memory: MCP call log is ON at {path} (privacy flip)", file=sys.stderr)


def start_call(message, headers=None):
    """Arm per-request detail when call logging is enabled. None leaves the request alone."""
    if enabled_path() is None:
        return None
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    if "id" not in message:
        return None
    secrets = _secrets(headers)
    return (
        _detail.set(
            {
                "started_at": datetime.now(UTC).isoformat(),
                # Stdio has no headers; HTTP always passes them.
                "transport": "stdio" if headers is None else "http",
                "secrets": secrets,
            }
        ),
        # Known before any text is cut, so a credential is never half-truncated.
        _pattern.set(_compile(secrets)),
    )


def note_call(**fields):
    detail = _detail.get()
    if detail is None:
        return
    try:
        _note(detail, fields)
    except Exception as exc:
        detail["note_failed"] = type(exc).__name__
        log.error("mcp call log note failed: %s", type(exc).__name__)


def _note(detail, fields):
    if "arguments" in fields:
        detail["arguments"] = _snapshot(fields.pop("arguments"))
    if "output" in fields:
        output = fields.pop("output")
        detail["compact"] = compact_output(
            output,
            image_omitted=bool(fields.get("image_omitted")),
            secrets=detail.get("secrets", ()),
        )
    if "validation" in fields:
        detail["validation"] = _validation(fields.pop("validation"))
    if "handler_ms" in fields:
        detail["handler_ms"] = round(fields.pop("handler_ms"), 3)
    detail.update(fields)


def finish_call(token, message, status, response, started):
    if token is None:
        return
    try:
        path = enabled_path()
        if path is None:
            return
        detail = _detail.get() or {}
        try:
            line = _line(_record(message, detail, status, response, started))
        except RecursionError:
            line = _line(
                {
                    **_minimal(message, detail, status, response, started),
                    "record_error": "RecursionError",
                }
            )
        if len(line) > LINE_LIMIT:
            line = _line(
                {**_minimal(message, detail, status, response, started), "record_truncated": True}
            )
        append_jsonl(path, line)
    except Exception as exc:
        # Type only: the record holds the question the operator opted into storing.
        log.error("mcp call log failed: %s", type(exc).__name__)
    finally:
        _detail.reset(token[0])
        _pattern.reset(token[1])


def _line(record):
    # ASCII escapes U+2028/U+2029/U+0085 and bidi controls that split or disguise lines.
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)


def append_jsonl(path, line):
    """Append one line, rotating `<path>` to `<path>.1` once the live file reaches the cap.

    Several processes (the HTTP server and each stdio session) can share a path, so
    rotation happens under an exclusive flock on `<path>.lock`, re-checked inside.
    """
    data = line.encode("utf-8")
    if not data.endswith(b"\n"):
        data += b"\n"
    target = Path(path)
    if target.is_dir():
        raise IsADirectoryError(errno.EISDIR, "call log path is a directory")
    for parent in reversed([target.parent, *target.parent.parents]):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    lock_path = target.with_name(target.name + ".lock")
    with _LOCK:
        lock_fd = _open_private(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                full = target.lstat().st_size >= MAX_CALL_LOG_BYTES
            except FileNotFoundError:
                full = False
            if full:
                # Raises when rotation is impossible: skip the record, never grow past the cap.
                os.replace(target, target.with_name(target.name + ".1"))
            fd = _open_private(target, os.O_APPEND | os.O_CREAT | os.O_RDWR)
            try:
                start = os.fstat(fd).st_size
                if start and os.pread(fd, 1, start - 1) != b"\n":
                    # A torn earlier write: start this record on its own line.
                    data = b"\n" + data
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError(errno.EIO, "short write")
                        view = view[written:]
                except OSError:
                    os.ftruncate(fd, start)
                    raise
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)


def _open_private(path, flags):
    """Open a regular file owned by this user without following links or blocking on a FIFO."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "call log path is not a regular file")
        if info.st_uid != os.getuid():
            raise OSError(errno.EPERM, "call log file belongs to another user")
        if info.st_mode & 0o777 != 0o600:
            os.fchmod(fd, 0o600)
        os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def compact_output(output, image_omitted=False, secrets=()):
    """Fact ids by lane and rank, entity keys, counts, and status. No prose or image bytes."""
    if not isinstance(output, dict):
        return {"preview": _truncate(str(output), TEXT_LIMIT)}
    result = {}
    if image_omitted or _contains_image(output):
        result["image_omitted"] = True
    result.update(_status_fields(output, secrets))
    if isinstance(output.get("question_terms"), list):
        # The terms repeat the question; their count shows an unranked (stop-word) query.
        result["question_term_count"] = len(output["question_terms"])
    returned, extra = _returned_facts(output)
    if returned:
        result["facts"] = returned[:ID_LIMIT]
        result["fact_ids"] = list(dict.fromkeys(row[0] for row in returned))[:ID_LIMIT]
        if len(returned) > ID_LIMIT:
            result["facts_truncated"] = True
    result.update(extra)
    entities = _entities(output)
    if entities:
        result["entities"] = entities[:20]
    return _cap_result(result)


def _returned_facts(output):
    """[id, lane, role] for each fact the client received, in rank order, plus id lists
    that were referenced but not returned (conflict sides, derived support, missing)."""
    rows = []
    extra = {}
    # A recalled fact about none of the resolved entities came in as a related decision.
    roots = output.get("entities")
    roots = (
        {e.get("key") for e in roots if isinstance(e, dict)} if isinstance(roots, list) else set()
    )
    seen = set()
    for name in _FACT_LISTS:
        items = output.get(name)
        if not isinstance(items, list):
            continue
        for item in items:
            fid = _fact_id(item)
            if fid is None or (name, fid) in seen:
                continue
            seen.add((name, fid))
            lane = item.get("lane") if isinstance(item.get("lane"), str) else None
            lane = lane or {"latest_facts": "latest", "facts": None}.get(name, name)
            row = [fid] if lane is None else [fid, _truncate(lane, KEY_LIMIT)]
            if roots and _related(item, roots):
                row.append("related")
            rows.append(row)
    for name in ("conflict_fact_ids", "missing_fact_ids", "related_fact_ids"):
        ids = _id_list(output.get(name))
        if ids:
            extra[name] = ids[:ID_LIMIT]
    support = []
    for name in _DERIVED_LISTS:
        for item in output.get(name) or []:
            if isinstance(item, dict):
                support.extend(_id_list(item.get("supporting_fact_ids")))
    if support:
        extra["derived_support_ids"] = list(dict.fromkeys(support))[:ID_LIMIT]
    ids = list(dict.fromkeys(_id_list(output.get("fact_ids"))))
    if ids and not rows:
        # Mutations report the facts they wrote or touched as a bare id list.
        extra["written_fact_ids"] = ids[:ID_LIMIT]
    return rows, extra


def _related(item, roots):
    ends = [item.get(k) for k in ("subject", "target") if isinstance(item.get(k), str)]
    return bool(ends) and not roots.intersection(ends)


def _fact_id(item):
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("fact"), dict):
        item = item["fact"]  # memory_evidence wraps each fact
    fid = item.get("id", item.get("fact_id"))
    if isinstance(fid, bool) or not isinstance(fid, (str, int)):
        return None
    return _truncate(str(fid), KEY_LIMIT)


def _id_list(value):
    if not isinstance(value, list):
        return []
    return [
        _truncate(str(v), KEY_LIMIT)
        for v in value
        if isinstance(v, (str, int)) and not isinstance(v, bool)
    ]


def _record(message, detail, status, response, started):
    secrets = detail.get("secrets", ())
    arguments, truncated = _bound_arguments(
        detail.get("arguments", _raw_arguments(message)),
        invalid=bool(detail.get("arguments_invalid")),
        secrets=secrets,
    )
    record = _minimal(message, detail, status, response, started)
    ok = record["ok"]
    result = dict(detail.get("compact") or {})
    if not ok:
        result.update(_failure(status, response, detail, secrets))
    elif "http_status" not in result:
        result["http_status"] = status
    record["arguments"] = arguments
    record["result"] = _scrub(_cap_result(result), secrets)
    if truncated:
        record["arguments_truncated"] = True
    return _scrub(record, secrets)


def _minimal(message, detail, status, response, started):
    """Who, when, and how the call ended. Never the arguments or the result."""
    secrets = detail.get("secrets", ())
    ok = _ok(status, response)
    tool = detail.get("tool") if isinstance(detail.get("tool"), str) else _raw_tool(message)
    record = {
        "ts": detail.get("started_at") or datetime.now(UTC).isoformat(),
        "request_id": _request_id(message),
        "tool": tool if tool is None else _key(tool, secrets),
        "ok": ok,
        "isError": not ok,
        "http_status": status,
        "timing_ms": round((time.perf_counter() - started) * 1000, 3),
        "transport": detail.get("transport"),
        "pid": os.getpid(),
        "server_version": __version__,
    }
    if "handler_ms" in detail:
        record["handler_ms"] = detail["handler_ms"]
    client = _client(message, secrets)
    if client:
        record["client"] = client
    for name in ("correlation_id", "note_failed"):
        if detail.get(name):
            record[name] = detail[name]
    return _scrub(record, secrets)


def _client(message, secrets):
    params = message.get("params") if isinstance(message, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    info = meta.get("io.modelcontextprotocol/clientInfo") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None
    return {k: _key(info[k], secrets) for k in ("name", "version") if isinstance(info.get(k), str)}


def _ok(status, response):
    if status != 200 or not isinstance(response, dict):
        return False
    result = response.get("result")
    return isinstance(result, dict) and not result.get("isError")


def _failure(status, response, detail, secrets):
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
    return failure


def _error_text(response):
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return None
    for block in result["content"]:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
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
    keys = [_key(k, secrets) for k in list(arguments)[:30]]
    return {"_truncated": True, "keys": keys}, True


def _key(key, secrets):
    return _truncate(str(key), KEY_LIMIT)


def _walk(value, limit, secrets, drop_heavy, depth=0):
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, list):
        if depth >= MAX_DEPTH:
            return {"_truncated": True, "count": len(value)}
        items = [_walk(item, limit, secrets, drop_heavy, depth + 1) for item in value[:40]]
        if len(value) > 40:
            return {"items": items, "count": len(value), "truncated": True}
        return items
    if isinstance(value, dict):
        if _is_image(value):
            return {"image_omitted": True, "chars": _image_chars(value)}
        if depth >= MAX_DEPTH:
            return {"_truncated": True}
        out = {}
        for key, item in list(value.items())[:60]:
            name = _key(key, secrets)
            if _is_secret_key(key):
                out[name] = "[redacted]"
            elif _is_blob_key(key) or _is_image(item):
                out[name] = {"image_omitted": True, "chars": _image_chars(item)}
            elif str(key).lower() in _ARG_PROSE or (drop_heavy and str(key).lower() in _HEAVY_KEYS):
                out[name] = _omitted(item)
            else:
                out[name] = _walk(item, limit, secrets, drop_heavy, depth + 1)
        if len(value) > 60:
            out["_keys_truncated"] = len(value)
        return out
    return _truncate(str(value), limit)


def _status_fields(value, secrets=(), depth=0):
    """Scalars and small scalar lists, minus prose and the structures walked elsewhere."""
    if not isinstance(value, dict) or depth > 3:
        return {}
    out = {}
    for key, item in list(value.items())[:60]:
        name = _key(key, secrets)
        lowered = str(key).lower()
        if (
            _is_secret_key(key)
            or _is_blob_key(key)
            or lowered in _RESULT_PROSE
            or _is_image(item)
            or (lowered in _STRUCTURE and isinstance(item, (list, dict)))
        ):
            continue
        if isinstance(item, bool) or item is None or isinstance(item, int):
            out[name] = item
        elif isinstance(item, float) and item == item and abs(item) != float("inf"):
            out[name] = item
        elif isinstance(item, str):
            out[name] = _truncate(item, TEXT_LIMIT)
        elif isinstance(item, dict):
            child = _status_fields(item, secrets, depth + 1)
            if child:
                out[name] = child
        elif (
            isinstance(item, list)
            and item
            and all(
                isinstance(entry, (bool, int, float, str)) or entry is None for entry in item[:20]
            )
        ):
            out[name] = [
                _truncate(entry, TEXT_LIMIT) if isinstance(entry, str) else entry
                for entry in item[:20]
            ]
            if len(item) > 20:
                out[name + "_truncated"] = True
    return out


def _entities(output):
    """Entity keys a result named, including those search resolved and fact subjects."""
    found = {}

    def add(key, name=None, kind=None, match=None):
        if not isinstance(key, str) or key in found or len(found) >= 40:
            return
        row = {"key": _truncate(key, 120)}
        for field, value in (("name", name), ("kind", kind), ("match", match)):
            if isinstance(value, str):
                row[field] = _truncate(value, 120)
        found[key] = row

    for name in ("entity", "resolved_entities", "entities", "candidates", "matches"):
        items = output.get(name)
        items = [items] if isinstance(items, dict) else items
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                add(item.get("key"), item.get("name"), item.get("kind"), item.get("match"))
    # memory_search drops its entity list; the facts it returned still name them.
    for fact in output.get("facts") or []:
        if isinstance(fact, dict):
            add(fact.get("subject"))
            add(fact.get("target"))
    return list(found.values())


def _contains_image(value, depth=0):
    if depth > MAX_DEPTH:
        return False
    if _is_image(value):
        return True
    if isinstance(value, dict):
        return any(
            _is_blob_key(str(key)) or _contains_image(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_image(item, depth + 1) for item in value)
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


def _secrets(headers):
    """Credentials this process knows: its own environment and the Authorization header
    (which, on a token server, had to equal the token to get here). Never argument
    values or other headers, which a client could fill to blank arbitrary words."""
    found = [os.environ.get(name) for name in SECRET_ENV]
    try:
        found.append(urlsplit(os.environ.get("NEO4J_URI", "")).password)
    except ValueError:
        pass
    if isinstance(headers, dict):
        found += [v for k, v in headers.items() if str(k).lower() == "authorization"]
    secrets = []
    for value in found:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        secrets.append(value)
        parts = value.split(None, 1)
        if len(parts) == 2:
            secrets.append(parts[1].strip())
    # The public default is not a secret; scrubbing it would blank every "graph-memory".
    # Matched with re.IGNORECASE; lowering here would break secrets such as "İ".
    usable = {s for s in secrets if len(s) >= 8 and s != DEFAULT_PASSWORD}
    return tuple(sorted(usable, key=len, reverse=True))


def _scrub(value, secrets, depth=0) -> Any:
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, int) and not isinstance(value, bool) and str(value) in secrets:
        return "[redacted]"
    if depth > MAX_DEPTH + 4:
        return {"_truncated": True}
    if isinstance(value, list):
        return [_scrub(item, secrets, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            _scrub_string(str(key)): _scrub(item, secrets, depth + 1) for key, item in value.items()
        }
    return value


def _scrub_string(text, secrets=None):
    """Case-insensitive replacement in one regex pass: linear, and index-safe where
    lower() changes a string's length. Uses this request's pattern unless given secrets."""
    if not text:
        return text
    pattern = _pattern.get() if secrets is None else _compile(secrets)
    return pattern.sub("[redacted]", text) if pattern else text


def _compile(secrets):
    return re.compile("|".join(map(re.escape, secrets)), re.IGNORECASE) if secrets else None


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
                "loc": [_truncate(str(part), KEY_LIMIT) for part in loc[:8]]
                if isinstance(loc, (list, tuple))
                else [],
                "msg": _truncate(str(item.get("msg", "")), TEXT_LIMIT),
            }
        )
    return rows


def _cap_result(result):
    if _size(result) <= RESULT_JSON_LIMIT:
        return result
    kept = {
        key: result[key]
        for key in (
            "http_status",
            "status",
            "ok",
            "revision",
            "as_of",
            "next_offset",
            "counts",
            "question_term_count",
            "image_omitted",
            "error",
            "error_code",
            "error_type",
            "validation",
        )
        if key in result
    }
    kept["result_truncated"] = True
    # Keep as much of each list as fits, most useful first, instead of fixed cut-offs.
    for name in (
        "facts",
        "fact_ids",
        "conflict_fact_ids",
        "missing_fact_ids",
        "derived_support_ids",
        "written_fact_ids",
        "entities",
    ):
        items = result.get(name)
        if not isinstance(items, list):
            continue
        if name == "entities":
            items = [{"key": row["key"]} for row in items if isinstance(row, dict)]
        room = len(items)
        while room and _size({**kept, name: items[:room]}) > RESULT_JSON_LIMIT:
            room = room * 3 // 4
        if room:
            kept[name] = items[:room]
        if room < len(items):
            kept[name + "_truncated"] = len(items)
    return kept


def _size(value):
    return len(json.dumps(value, default=str))


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
    return _truncate(request_id, KEY_LIMIT) if isinstance(request_id, str) else request_id


def _truncate(text, limit):
    # Scrub before cutting: a credential cut in half would no longer match.
    text = _scrub_string(text)
    if len(text) <= limit:
        return text
    if limit <= len(MARKER):
        return MARKER
    return text[: limit - len(MARKER)] + MARKER


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
