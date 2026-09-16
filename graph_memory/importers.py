"""Read-only adapters. Markdown is an import format, never a retrieval dependency."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .models import Message, Transcript


def redact(text: str) -> str:
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[\w-]+)\b",
        "[REDACTED TOKEN]",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:password|passwd|api[_-]?key|access[_-]?token|secret|authorization)\b\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{12,}", r"\1[REDACTED]", text)
    return text


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def read_messages(path: Path, allow_incomplete=False):
    if path.suffix.lower() in (".md", ".txt"):
        text = redact(path.read_text())
        # Preserve full text, split bounded inputs without pretending mtime is event time.
        for i in range(0, len(text), 24_000):
            part = text[i : i + 24_000]
            if part.strip():
                yield Message(id=f"note-{i}", role="note", content=part)
        return
    if path.suffix.lower() != ".jsonl":
        raise ValueError("Expected .md, .txt, or Claude/Codex .jsonl transcript")
    with path.open() as file:
        for index, line in enumerate(file):
            if allow_incomplete and not line.endswith("\n"):
                break
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed transcript JSON at line {index + 1}") from exc
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "response_item":  # Codex: event_msg would duplicate these.
                message = item.get("payload", {})
                if message.get("type") != "message":
                    continue
            elif kind in ("user", "assistant"):  # Claude Code
                message = item.get("message", {})
            else:
                continue
            role = message.get("role", kind)
            if role not in ("user", "assistant"):
                continue
            content = redact(text_content(message.get("content")))
            if not content.strip():
                continue
            for offset in range(0, len(content), 24_000):
                yield Message(
                    id=f"line-{index + 1}-{offset}",
                    role=role,
                    content=content[offset : offset + 24_000],
                    timestamp=item.get("timestamp"),
                )


def transcripts(path: Path, namespace: str):
    identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    stat = path.stat()
    document = path.suffix.lower() in (".md", ".txt")
    created = getattr(stat, "st_birthtime", None)
    batch, size, part = [], 0, 0

    def make(messages, number):
        return Transcript(
            namespace=namespace,
            source_id=f"file:{identity}:{number}",
            session_id=f"file:{identity}",
            source_uri=str(path.resolve()),
            source_kind="transcript" if path.suffix == ".jsonl" else "memory_import",
            source_created_at=datetime.fromtimestamp(created, UTC)
            if document and created
            else None,
            source_updated_at=datetime.fromtimestamp(stat.st_mtime, UTC) if document else None,
            messages=messages,
        )

    for message in read_messages(path):
        if batch and (size + len(message.content) > 30_000 or len(batch) >= 100):
            yield make(batch, part)
            batch, size, part = [], 0, part + 1
        batch.append(message)
        size += len(message.content)
    if batch:
        yield make(batch, part)


def memory_files(roots: list[Path]):
    files = set()
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            # A Claude projects directory includes large transcripts and tool outputs;
            # its memory bank is specifically the memory/ directories.
            pattern = (
                "*/memory/*.md"
                if root.name == "projects" and root.parent.name == ".claude"
                else "**/*.md"
            )
            files.update(
                p.resolve() for p in root.glob(pattern) if p.is_file() and not p.is_symlink()
            )
        else:
            raise ValueError(f"Import path does not exist: {root}")
    return sorted(files)
