"""Transcript evidence adapters. Never open files mentioned by a tool call.

Only source JSONL is read. Reasoning, injected system/developer instructions and
provider duplicate event mirrors are not conversational evidence. Grok Bot desk
lines are Claude-shaped, or flat records that name fromAgent or channel. A
user-role line from another agent is a relay, not a direct assertion.
"""

import ast
import functools
import json
import posixpath
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from .importers import redact, redact_v1, text_content
from .models import ArtifactTouch, Message, Transcript
from .store import digest

FORMAT = "session-records-v1"
GROK_BOT = "grok-bot"
CHUNK = 24_000
MAX_BATCH_CHARS = 90_000
# Earlier tool results of the turn in progress, carried into a batch as context: an
# agent reports after its tool calls, and the results that back the report are
# usually many messages behind it.
LOOKBACK_CHARS = 20_000
LOOKBACK_MESSAGES = 400
OPAQUE_CALL = "opaque_command_artifacts_not_resolved"
SHELL_OUTPUT = "shell_output_not_attributed_to_files"
# path, operation, submitted body, capture: the ArtifactTouch vocabulary.
Touch = tuple[
    str,
    Literal["read", "write", "patch"],
    str,
    Literal["excerpt", "submitted_content", "patch", "unavailable"],
]


def memory_path(path):
    value = path.replace("\\", "/").lower()
    return (
        "/memories/" in value
        or "/memory/" in value
        or value.startswith(("memory/", "memories/"))
        or value.endswith("memory.md")
    )


def tool_touch(name, arguments) -> list[Touch]:
    """Conservative artifact recognition; unrecognized shell/JS stays an explicit gap."""
    short = name.rsplit("__", 1)[-1].lower()
    args = arguments if isinstance(arguments, dict) else {}
    path = args.get("file_path") or args.get("path")
    if isinstance(path, str) and memory_path(path):
        if short in {"read", "read_file"}:
            return [(path, "read", "", "excerpt")]
        if short in {"write", "write_file"}:
            return [(path, "write", str(args.get("content", "")), "submitted_content")]
        if short in {"edit", "multiedit", "edit_file"}:
            return [(path, "patch", json.dumps(args, ensure_ascii=False), "patch")]
    # apply_patch carries before/after lines, not a reconstructed file version.
    raw = arguments if isinstance(arguments, str) else args.get("patch", args.get("input", ""))
    if "apply_patch" in short and isinstance(raw, str):
        return [
            (p, "patch", raw, "patch")
            for p in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", raw, re.M)
            if memory_path(p)
        ]
    # Recognize literal nested exec arguments, without executing JavaScript.
    if short in {"exec", "functions.exec"} and isinstance(arguments, str):
        found = []
        literal = r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
        for match in re.finditer(r"""\b(?:cmd|command)\s*["']?\s*:\s*""" + literal, arguments):
            try:
                command = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                continue
            found.extend(tool_touch("exec_command", {"cmd": command}))
        for match in re.finditer(r"tools\.apply_patch\(\s*" + literal, arguments):
            try:
                patch = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                continue
            found.extend(tool_touch("apply_patch", patch))
        return list(dict.fromkeys(found))
    # Static shell read references; output extent remains explicitly unproven.
    command = args.get("command", args.get("cmd", ""))
    if isinstance(command, str):
        import shlex

        found = []
        for part in re.split(r"[\n;|&]", command):
            if re.search(r"[`$<>]", part):
                continue
            try:
                words = shlex.split(part)
            except ValueError:
                continue
            if words and words[0] in {"cat", "sed", "head", "tail", "rg", "grep"}:
                found.extend(
                    (p, "read", "", "excerpt")
                    for p in words[1:]
                    if memory_path(p) and p.endswith((".md", ".txt"))
                )
        return list(dict.fromkeys(found))
    return []


def episode_format(path: Path, source_key=None) -> str:
    """Episode evidence format, from the feed's root label when the watcher named
    one, else from the path. Feed cursors stay session-records-v1."""
    if source_key:
        return GROK_BOT if source_key.startswith(GROK_BOT + ":") else FORMAT
    return GROK_BOT if GROK_BOT in path.parts else FORMAT


def agent_ref(item):
    """Another agent named on the line, by id when the record has one. Any
    fromAgent other than null names one, even without a usable id."""
    agent = item.get("fromAgent")
    if agent is None:
        return None
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    if isinstance(agent, dict):
        for key in ("id", "name"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unknown-agent"


def channel_name(item):
    channel = item.get("channel")
    if isinstance(channel, str) and channel.strip():
        return channel.strip()
    if isinstance(channel, dict):
        for key in ("id", "name"):
            value = channel.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def bounded_label(value):
    if not value:
        return None
    return " ".join(value.split())[:200] or None


def is_grok_native(item):
    """A flat grok-bot line, not a Claude or Codex record."""
    kind = item.get("type")
    if kind in {
        "user",
        "assistant",
        "response_item",
        "session_meta",
        "turn_context",
        "compacted",
        "event_msg",
    }:
        return False
    if item.get("source_format") == GROK_BOT or item.get("source") == GROK_BOT:
        return True
    if item.get("role") not in {"user", "assistant"} and not isinstance(
        item.get("content"), (str, list)
    ):
        return False
    return any(key in item for key in ("fromAgent", "fromUser", "channel"))


AWARE_TIME = TypeAdapter(AwareDatetime)


def record_time(item):
    """The line's event time, or None. A timestamp without a zone, or one that
    does not parse, is missing rather than a reason to stop reading the file."""
    stamp = item.get("timestamp")
    if stamp is not None:
        try:
            return AWARE_TIME.validate_python(stamp)
        except ValidationError:
            return None
    ms = item.get("timestampMs")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        try:
            return datetime.fromtimestamp(ms / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def grok_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks = []
    for block in content:
        if isinstance(block, str):
            blocks.append({"type": "text", "text": block})
        elif isinstance(block, dict):
            blocks.append(block)
    return blocks


def as_claude(item):
    """Flat grok-bot text as a Claude-shaped record. None when the line has no text."""
    role = item.get("role")
    if role not in {"user", "assistant"}:
        return None
    blocks = grok_blocks(item.get("content", item.get("text", "")))
    if not text_content(blocks).strip():
        return None
    shaped = {
        "type": role,
        "timestamp": record_time(item),
        "message": {"role": role, "content": blocks},
    }
    for key in ("isSidechain", "fromAgent", "fromUser", "channel", "sessionId", "cwd"):
        if key in item:
            shaped[key] = item[key]
    return shaped


def records(path: Path, scrub=redact):
    """Stable IDs use line/block/chunk positions, including tool arguments/results.
    scrub is the redaction; only cursor checks pass an older one."""
    calls = {}
    cwd = None
    delegated = "subagents" in path.parts
    automated = False
    for line_number, line in enumerate(path.open(), 1):
        if not line.endswith("\n"):
            break  # writer may still be appending this record
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed transcript JSON at line {line_number}") from exc
        if not isinstance(item, dict):
            continue
        if is_grok_native(item):
            item = as_claude(item)
            if item is None:
                continue
        kind = item.get("type")
        if kind in {"session_meta", "turn_context"}:
            cwd = item.get("payload", {}).get("cwd", cwd)
        if kind == "session_meta":
            origin = item.get("payload", {}).get("source")
            delegated = delegated or (isinstance(origin, dict) and "subagent" in origin)
            automated = origin == "exec"
        cwd = item.get("cwd", cwd)
        payload = item.get("payload", {}) if kind == "response_item" else item.get("message", {})
        entries = []
        if kind == "response_item" and isinstance(payload, dict):
            t = payload.get("type")
            if t in {"function_call", "custom_tool_call"}:
                entries = [("call", payload)]
            elif t in {"function_call_output", "custom_tool_call_output"}:
                entries = [("result", payload)]
            elif t == "message" and payload.get("role") in {"user", "assistant"}:
                entries = [("text", payload)]
        elif kind in {"user", "assistant"} and isinstance(payload, dict):
            content = payload.get("content", [])
            if isinstance(content, str):
                entries = [("text", {**payload, "role": payload.get("role", kind)})]
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    t = block.get("type")
                    if t == "tool_use":
                        entries.append(("call", {**block, "call_id": block.get("id")}))
                    elif t == "tool_result":
                        entries.append(
                            (
                                "result",
                                {
                                    **block,
                                    "call_id": block.get("tool_use_id"),
                                    "output": block.get("content"),
                                },
                            )
                        )
                    elif t in {"text", "input_text", "output_text"}:
                        entries.append(
                            (
                                "text",
                                {
                                    "role": payload.get("role", kind),
                                    "content": block.get("text", ""),
                                },
                            )
                        )
                    elif t in {"image", "document"}:
                        entries.append(
                            (
                                "attachment",
                                {
                                    "role": "note",
                                    "content": "[Non-text attachment unavailable to transcript text extraction]",
                                },
                            )
                        )
        elif kind == "compacted":
            # A compaction summary is derived context, not a new user statement.
            entries = [
                (
                    "context",
                    {
                        "role": "note",
                        "content": item.get("message", "[Compacted source context unavailable]"),
                    },
                )
            ]
        for block_number, (entry_kind, entry) in enumerate(entries):
            record_id = f"line-{line_number}-block-{block_number}"
            call_id = entry.get("call_id")
            tool = None
            failed = None
            touches = []
            gaps = []
            agent = None
            desk = None
            if entry_kind == "call":
                tool = entry.get("name", "unknown tool")
                args = entry.get("arguments", entry.get("input", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                recognized: list[Touch] = tool_touch(tool, args)
                call_cwd = (
                    args.get("workdir", args.get("cwd", cwd)) if isinstance(args, dict) else cwd
                )
                recognized = [
                    (
                        posixpath.normpath(posixpath.join(call_cwd, p))
                        if call_cwd and not p.startswith(("/", "~"))
                        else p,
                        op,
                        body,
                        captured,
                    )
                    for p, op, body, captured in recognized
                ]
                if len(recognized) > 32:
                    gaps.append("artifact_reference_limit_exceeded")
                    recognized = recognized[:32]
                # Keep pairing state immutable with respect to later results.
                shell = any(x in tool.lower() for x in ("exec", "bash", "shell", "python"))
                content = (
                    json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
                )
                if call_id:
                    calls[call_id] = (tool, recognized, shell and "memor" in content.lower())
                role, source_type = "assistant", "tool_call"
                if not recognized and shell:
                    gaps.append(OPAQUE_CALL)
                for p, op, body, captured in recognized:
                    touches.append(
                        ArtifactTouch(
                            path=p,
                            operation=op,
                            content=scrub(body)[:CHUNK],
                            captured=captured if body else "unavailable",
                            gap="execution_not_yet_observed",
                        )
                    )
            elif entry_kind == "result":
                tool, recognized, reads_memory = calls.get(call_id, ("unknown tool", [], False))
                output = entry.get("output", "")
                if isinstance(output, list) and any(
                    isinstance(x, dict)
                    and x.get("type") not in {"text", "input_text", "output_text"}
                    for x in output
                ):
                    gaps.append("non_text_tool_output")
                content = (
                    text_content(output)
                    if isinstance(output, list)
                    else output
                    if isinstance(output, str)
                    else json.dumps(output, ensure_ascii=False)
                )
                role, source_type = "tool", "tool_result"
                failed = entry.get("is_error")
                if call_id not in calls:
                    gaps.append("tool_call_not_present")
                    source_type = "context"
                if any(op == "read" for _, op, _, _ in recognized):
                    source_type = "memory_read"
                elif recognized:
                    source_type = "memory_write"
                if "memory" in tool.lower():
                    if any(
                        x in tool.lower() for x in ("recall", "latest", "search", "read", "get")
                    ):
                        source_type = "memory_read"
                        gaps.append("derived_memory_retrieval_not_fresh_verification")
                    elif any(
                        x in tool.lower()
                        for x in ("ingest", "write", "merge", "retract", "add", "delete")
                    ):
                        source_type = "memory_write"
                if any(
                    x in tool.lower()
                    for x in ("spawn_agent", "wait_agent", "send_message", "followup_task")
                ):
                    source_type = "context"
                    gaps.append("delegated_agent_report_not_execution_evidence")
                if tool == "unknown tool":
                    gaps.append("tool_result_semantics_unknown")
                for p, op, body, captured in recognized:
                    compound = any(x in tool.lower() for x in ("exec", "bash", "shell", "python"))
                    captured_content = ("" if compound else content) if op == "read" else body
                    if len(captured_content) > CHUNK:
                        gaps.append("artifact_excerpt_truncated_see_source_chunks")
                    touches.append(
                        ArtifactTouch(
                            path=p,
                            operation=op,
                            content=scrub(captured_content)[:CHUNK],
                            captured=("unavailable" if compound else "excerpt")
                            if op == "read"
                            else captured,
                            gap=(
                                "compound_output_not_attributed_to_individual_file"
                                if compound
                                else "read_extent_not_proven"
                            )
                            if op == "read"
                            else "write_request_not_independent_confirmation",
                        )
                    )
                if not recognized and any(
                    x in tool.lower() for x in ("exec", "bash", "shell", "python")
                ):
                    # A command's output is what the agent observed, and it is how agents
                    # verify most things. Only a command that names memory is held back:
                    # what it printed may be a stored claim, not a fresh observation.
                    if reads_memory or source_type != "tool_result":
                        source_type = "context"
                        gaps.append(OPAQUE_CALL)
                    else:
                        gaps.append(SHELL_OUTPUT)
            else:
                role = entry.get("role", "note")
                content = text_content(entry.get("content", ""))
                # Redacted before truncation, so a cut cannot split a secret.
                agent = bounded_label(scrub(agent_ref(item) or ""))
                desk = bounded_label(scrub(channel_name(item) or ""))
                source_type = "user_assertion" if role == "user" else "assistant_report"
                # Sidechain text and another agent's user-role line are relays.
                # They are not the person's assertions.
                relay = delegated or bool(item.get("isSidechain")) or automated or bool(agent)
                if role == "user" and relay:
                    source_type = "context"
                    gaps.append(
                        "delegated_instruction"
                        if delegated or item.get("isSidechain") or agent
                        else "automated_prompt_not_direct_user_assertion"
                    )
                if entry_kind in {"context", "attachment"}:
                    source_type = "context"
                    gaps.append(
                        "derived_context" if entry_kind == "context" else "non_text_attachment"
                    )
            content = scrub(content)
            if not content.strip():
                content = "[Empty tool or source record]"
            for offset in range(0, len(content), CHUNK):
                chunk = content[offset : offset + CHUNK]
                # Validation strips whitespace. A boundary can leave an empty
                # chunk even when the complete record contains useful text.
                # Keep source offsets stable for subsequent chunks.
                if not chunk.strip():
                    continue
                yield Message(
                    id=f"{record_id}-{offset}",
                    record_id=record_id,
                    role=role,
                    content=chunk,
                    timestamp=record_time(item),
                    source_type=source_type,
                    call_id=call_id,
                    tool_name=tool,
                    tool_failed=failed,
                    touches=touches if offset == 0 else [],
                    gaps=gaps + (["record_split_into_chunks"] if len(content) > CHUNK else []),
                    from_agent=agent,
                    channel=desk,
                )


def cursor_matches(path: Path, messages, count, stored):
    """Whether a cursor still describes the file's first count messages.

    A cursor hashes redacted text, so it may predate shell output validating a
    claim (before_shell_results) or the current redaction patterns (redact_v1).
    Both forms of the file it was written from are accepted."""
    if count > len(messages):
        return False
    if not count:
        return True

    def hashes(found):
        prefix = [m if isinstance(m, dict) else m.model_dump(mode="json") for m in found[:count]]
        return digest(prefix), digest([before_shell_results(m) for m in prefix])

    if stored in hashes(messages):
        return True
    # Parsed again only on a mismatch. Inventory never advances a cursor, so an
    # idle file keeps its older hash; the answer is kept until the file changes.
    info = path.stat()
    return legacy_match(str(path), info.st_mtime_ns, info.st_size, count, stored)


@functools.lru_cache(maxsize=4096)
def legacy_match(path, mtime_ns, size, count, stored):
    prefix = [m.model_dump(mode="json") for m in list(records(Path(path), redact_v1))[:count]]
    return stored in (digest(prefix), digest([before_shell_results(m) for m in prefix]))


def as_stored(tx, namespace, session_id, path, selected, fed):
    """Already-fed messages as the graph holds them. A message stored under the
    earlier redaction keeps that text, so carrying it again as context does not
    read as a changed source. Any other difference is left for the source graph
    to refuse."""
    ids = {digest([namespace, session_id, m.id]): m for m in selected if m.id in fed}
    if not ids:
        return selected
    stored = {
        r["id"]: r["content"]
        for r in tx.run(
            "MATCH (m:MemoryMessage) WHERE m.id IN $ids RETURN m.id AS id, m.content AS content",
            ids=list(ids),
        )
    }
    changed = {ids[k].id: v for k, v in stored.items() if v != ids[k].content}
    if not changed:
        return selected
    earlier = {m.id: m.content for m in records(path, redact_v1) if m.id in changed}
    return [
        m.model_copy(update={"content": changed[m.id]})
        if m.id in changed and earlier.get(m.id) == changed[m.id]
        else m
        for m in selected
    ]


def before_shell_results(message):
    """The message as parsed before shell output could validate a claim. Cursors
    written then hash this form, and the file behind them has not changed."""
    if SHELL_OUTPUT not in message["gaps"]:
        return message
    gaps = [OPAQUE_CALL if g == SHELL_OUTPUT else g for g in message["gaps"]]
    return {**message, "source_type": "context", "gaps": gaps}


def turn_results(messages, count, held, room):
    """Successful tool results between the last user message and the batch, newest
    first, each with its call, until `room` characters are used."""
    calls = {m.call_id: m for m in messages[:count] if m.source_type == "tool_call" and m.call_id}
    carried = []
    for m in reversed(messages[max(0, count - LOOKBACK_MESSAGES) : count]):
        # Any user-role message ends the turn, including a delegated or automated
        # instruction; an earlier turn's memory reads are not this turn's context.
        if m.role == "user":
            break
        if m.source_type != "tool_result" or m.tool_failed is True or m.id in held:
            continue
        group = [x for x in (calls.get(m.call_id), m) if x and x.id not in held]
        size = sum(len(x.content) for x in group)
        if size > room:
            continue
        room -= size
        carried.extend(group)
        held.update(x.id for x in group)
    return carried


def batch(messages, count, lookback=LOOKBACK_CHARS):
    """The next bounded batch after `count`: where it ends, and its messages in
    source order with the context they are read against."""
    end, size = count, 0
    while end < len(messages) and end - count < 8:
        n = len(messages[end].content)
        if end > count and size + n > MAX_BATCH_CHARS:
            break
        size += n
        end += 1
    selected = messages[max(0, count - 4) : end]
    ids = {m.id for m in selected}
    selected += turn_results(messages, count, ids, lookback)
    result_calls = {m.call_id for m in messages[count:end] if m.role == "tool" and m.call_id}
    # Pair a late result with its call even if more than four messages apart.
    for m in messages[:count]:
        if m.source_type == "tool_call" and m.call_id in result_calls and m.id not in ids:
            if (
                sum(len(x.content) for x in selected) + len(m.content) <= 450_000
                and len(selected) < 490
            ):
                selected.append(m)
                ids.add(m.id)
    selected.sort(
        key=lambda m: (
            int(m.id.split("-")[1]),
            int(m.id.split("-")[3]),
            int(m.id.split("-")[4]),
        )
    )
    return end, selected


def feed_records(
    service,
    namespace,
    path: Path,
    session_id: str,
    max_batches=4,
    *,
    feed_id=None,
    source_key=None,
    session_uid=None,
):
    """Incremental, restart-safe intake into a separate source-model graph.

    Cursor versioning prevents accidentally resuming the old text-only parser.
    Batches are bounded; earlier tool calls are attached as context when needed.
    The watcher passes the feed it resolved for the file (see feed_identity), so a
    moved file keeps its cursor; without one the feed is named by path and session.
    """
    path = path.expanduser().resolve(strict=True)
    messages = list(records(path))
    from .recall_provenance import report_origins

    origins = report_origins(messages)
    fid = feed_id or digest([FORMAT, namespace, str(path), session_id])
    title_message = next(
        (
            m
            for m in messages
            if m.source_type == "user_assertion" and not m.content.lstrip().startswith("<")
        ),
        None,
    )
    title = " ".join(title_message.content.split())[:120] if title_message else path.stem[:120]

    def run(tx):
        service.store.lock(tx, namespace)
        row = tx.run("MATCH (f:MemoryFeed {id:$id}) RETURN properties(f) AS f", id=fid).single()
        previous = row["f"] if row else {}
        count = previous.get("message_count", 0)
        if not cursor_matches(path, messages, count, previous.get("prefix_hash")):
            raise ValueError(
                "Transcript prefix changed; preserve old evidence and review a new source revision"
            )
        receipts = []
        fed = {m.id for m in messages[:count]}
        while count < len(messages) and len(receipts) < max_batches:
            end, selected = batch(messages, count)
            selected = as_stored(tx, namespace, session_id, path, selected, fed)
            t = Transcript(
                namespace=namespace,
                session_id=session_id,
                source_id=f"records:{fid}:{count}",
                source_uri=str(path),
                source_format=episode_format(path, source_key),
                title=title,
                messages=selected,
                memory_origins={m.id: origins[m.id] for m in selected if m.id in origins},
                focus_message_ids=[m.id for m in messages[count:end]],
            )
            receipts.append(service.store.stage(t, transaction=tx))
            count = end
        tx.run(
            "MERGE (f:MemoryFeed {id:$id}) SET f.name=$name,f.namespace=$ns,f.session_id=$session,f.source_uri=$path,f.source_format=$format,f.message_count=$count,f.prefix_hash=$hash,"
            "f.source_key=coalesce($key,f.source_key),f.session_uid=coalesce($uid,f.session_uid)",
            id=fid,
            key=source_key,
            uid=session_uid,
            ns=namespace,
            session=session_id,
            path=str(path),
            format=FORMAT,
            name="Feed · " + title,
            count=count,
            hash=digest([m.model_dump(mode="json") for m in messages[:count]]),
        ).consume()
        return {
            "feed_id": fid,
            "message_count": count,
            "available_messages": len(messages),
            "caught_up": count == len(messages),
            "receipts": receipts,
        }

    return service.store.transaction(run)
