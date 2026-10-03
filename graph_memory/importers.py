"""Read-only adapters. Markdown is an import format, never a retrieval dependency."""

import bisect
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .models import Message, Transcript


def redact_v1(text: str) -> str:
    """The redaction feed cursors were hashed with before the patterns below were
    added. Frozen: a cursor written then must still match its file."""
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


# Assignment keys whose value is a credential. The lookbehind lets a prefix
# such as OPENAI_ or GITHUB_ stand before the key; a repeated prefix group here
# would backtrack exponentially on long runs of A_B_. A quote, escaped in
# JSON-encoded tool arguments, may close the key.
SECRET_KEYS = (
    r"(?:password|passwd|passphrase|secret|client[_-]?secret"
    r"|webhook[_-]?(?:key|secret)|api[_-]?key|apikey|access[_-]?token"
    r"|refresh[_-]?token|auth[_-]?token|private[_-]?key|authorization"
    # Environment names such as GITHUB_TOKEN; lowercase max_token is a setting.
    r"|(?-i:TOKEN))"
)
QUERY_KEYS = (
    r"(?:token|key|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
)
# Quoted values consume escaped delimiters as content. The first alternative
# handles a quoted value inside JSON-encoded tool arguments (\"...\"). Its
# interior distinguishes an encoded backslash (\\\\) from an escaped quote
# (\\\"). Disjoint, possessive repeats keep long escape runs linear. An
# unterminated quote consumes to the end of the line rather than leaking a tail.
QUOTED = (
    r'\\"(?:\\\\(?:\\\\|\\[^\n]|[^\\\n])|\\[^"\\\n]|[^\\\n])*+(?:\\"|(?=\n|\Z))'
    r'|"(?:\\[^\n]|[^"\\\n])*+(?:"|(?=\n|\Z))'
    r"|'(?:\\[^\n]|[^'\\\n])*+(?:'|(?=\n|\Z))"
)
# Interior punctuation is part of a secret; leave trailing structural closers
# when there is a non-punctuation value. The final assignment alternative below
# also covers values made entirely of closers, which redact_v1 removed.
MORE = r"[^\s,;]*[^\s,;\"'}\])\\]"
ASSIGNMENT = re.compile(
    r"(?i)((?<![A-Za-z0-9])" + SECRET_KEYS + r"\b(?:\\?[\"'])?\s*[=:]\s*"
    r"(?:(?:Bearer|Basic|Token)\s+)?)(?!(?:\\?[\"'])?\[REDACTED)"
    r"(?!(?i:Bearer|Basic|Token)\s)"
    rf"((?:{QUOTED})(?:{MORE})?|{MORE}|[^\s,;]+)"
)
QUERY = re.compile(r"(?i)([?&#]" + QUERY_KEYS + r"=)(?!\[REDACTED)[^&#\s\"'<>]+")
# GitHub device-flow user codes (XXXX-XXXX), only shortly after a prompt for
# one on the same line: a bare pattern also matches ticket keys, UUID parts and
# year ranges. The marker itself is not a prompt, so a second pass is a no-op.
DEVICE_CUE = re.compile(
    r"(?i)(?<!\[REDACTED )\b(?:device|user[_ -]?code|one[- ]time code|login/device"
    r"|enter (?:the |this |your )?code)\b"
)
DEVICE_CODE = re.compile(r"(?<![\w-])[A-Z0-9]{4}-[A-Z0-9]{4}(?![\w-])")


def assignment(m):
    value = m[2]
    quote = '\\"' if value.startswith('\\"') else value[0] if value[0] in "\"'" else ""
    close = quote if quote and len(value) >= 2 * len(quote) and value.endswith(quote) else ""
    return m[1] + quote + "[REDACTED]" + close


def device_codes(text):
    ends = [m.end() for m in DEVICE_CUE.finditer(text)]
    if not ends:
        return text

    def replace(m):
        i = bisect.bisect_right(ends, m.start()) - 1
        near = i >= 0 and m.start() - ends[i] <= 60 and "\n" not in text[ends[i] : m.start()]
        return "[REDACTED DEVICE CODE]" if near else m[0]

    return DEVICE_CODE.sub(replace, text)


def redact(text: str) -> str:
    """Strip credentials before any feed text is stored. Patterns apply to every source."""
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[\w-]+)\b",
        "[REDACTED TOKEN]",
        text,
    )
    # xAI keys are alphanumeric after the prefix; model names such as
    # xai-grok-2-vision carry hyphens and stay.
    text = re.sub(
        r"\b(?:tskey-[A-Za-z0-9_-]{8,}|xai-[A-Za-z0-9]{20,}|crsr_[A-Za-z0-9_-]{16,})\b",
        "[REDACTED TOKEN]",
        text,
    )
    # JWT and JWE: a JSON header, then two to four more segments. The last may be
    # empty (alg none).
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*){2,4}", "[REDACTED TOKEN]", text)
    text = QUERY.sub(r"\1[REDACTED]", text)
    text = ASSIGNMENT.sub(assignment, text)
    text = re.sub(r"(?i)((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", text)
    return device_codes(text)


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


def read_messages(path: Path, allow_incomplete=False, scrub=redact):
    if path.suffix.lower() in (".md", ".txt"):
        text = scrub(path.read_text())
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
            content = scrub(text_content(message.get("content")))
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
