"""Transcript feed identity that survives a change of mount point or host path.

A file is named by its source key, ``LABEL:relative/posix/path.jsonl``: the path
below the transcript root it was found in, prefixed by a label for that root.
The label comes from the root's name, skipping the generic directory names the
hosts use, so ``/sessions/claude`` and ``~/.claude/projects`` are both
``claude``. A root given as ``LABEL=PATH`` states its label instead.

Feeds staged before source keys existed keep the ids derived from their absolute
path: episodes, messages and the journal already reference them.
"""

import json
import os
import re
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .session_sources import FORMAT
from .store import digest

# Where the hosts keep sessions; the directory above names the host.
GENERIC = {"projects", "sessions"}
LABEL = re.compile(r"[A-Za-z0-9._-]+")
UID_LINES = 64


class Root(NamedTuple):
    label: str
    given: Path  # as mounted, links unresolved: what a walk yields paths under
    base: Path  # resolved directory that keys are relative to

    def key(self, found: Path):
        """The resolved path of a file found under this root, and its source key."""
        resolved = found.resolve()
        try:
            relative = resolved.relative_to(self.base)
        except ValueError:
            # A link out of the root is still named by where it sits in the root.
            relative = found.relative_to(self.given.parent if self.is_file() else self.given)
        return str(resolved), f"{self.label}:{relative.as_posix()}"

    def is_file(self):
        return self.given.is_file()


def derived_label(directory: Path):
    for name in reversed(directory.parts):
        name = name.lstrip(".")
        if name.lower() not in GENERIC and LABEL.search(name):
            return "_".join(LABEL.findall(name))
    return "root"


def parse_root(root) -> Root:
    text = str(root)
    label, stated, rest = text.partition("=")
    # An existing path that happens to contain "=" is a path, not a label.
    if stated and LABEL.fullmatch(label) and rest and not Path(text).exists():
        given = Path(os.path.abspath(Path(rest).expanduser()))
    else:
        label, given = "", Path(os.path.abspath(Path(text).expanduser()))
    directory = given.parent if given.is_file() else given
    return Root(label or derived_label(directory), given, directory.resolve())


def parse_roots(roots) -> list[Root]:
    """Most specific first, so overlapping roots name a file the same in any order."""
    parsed = [parse_root(root) for root in roots]
    owners = {}
    for root in parsed:
        if owners.setdefault(root.label, root.base) != root.base:
            raise ValueError(
                f"Transcript roots {owners[root.label]} and {root.base} share the label "
                f"{root.label!r}; give one of them as LABEL=PATH"
            )
    return sorted(parsed, key=lambda root: -len(root.base.parts))


def source_files(roots) -> dict[str, str]:
    """Resolved path -> source key for every transcript below the roots."""
    found = {}
    for root in parse_roots(roots):
        files = [root.given] if root.is_file() else root.given.rglob("*.jsonl")
        for file in files:
            name, key = root.key(file)
            found.setdefault(name, key)
    return found


def session_uid(path: Path):
    """The session's own id as its records state it. Informational: subagent
    transcripts repeat their parent's id, so it cannot name a file."""
    try:
        with path.open() as stream:
            for line in islice(stream, UID_LINES):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                uid = item.get("sessionId")
                if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                    uid = item["payload"].get("id")
                if isinstance(uid, str) and uid:
                    return uid[:200]
    except (OSError, ValueError):
        pass
    return None


class Identity(NamedTuple):
    feed_id: str
    session_id: str
    source_key: str
    known: bool


def feed_rows(tx, namespace):
    return tx.run(
        "MATCH (f:MemoryFeed {namespace:$ns,source_format:$format}) "
        "RETURN f.id AS id,f.session_id AS session,f.source_key AS key,f.source_uri AS uri",
        ns=namespace,
        format=FORMAT,
    ).data()


class Feeds:
    """The namespace's feed cursors, by source key and, for unstamped ones, by path."""

    def __init__(self, store, namespace):
        self.namespace = namespace
        self.by_key, self.by_uri = {}, {}
        for row in store.read(feed_rows, namespace):
            if row["key"]:
                self.by_key[row["key"]] = row
            # The watcher's own feed for a path wins over one a direct caller made.
            elif row["uri"] not in self.by_uri or row["session"] == "host:" + digest(row["uri"]):
                self.by_uri[row["uri"]] = row

    def resolve(self, key, name) -> Identity:
        row = self.by_key.get(key) or self.by_uri.get(name)
        if row:
            return Identity(row["id"], row["session"], key, True)
        session = "source:" + digest(key)
        return Identity(digest([FORMAT, self.namespace, key, session]), session, key, False)

    def record(self, identity: Identity, name):
        self.by_uri.pop(name, None)
        self.by_key[identity.source_key] = {
            "id": identity.feed_id,
            "session": identity.session_id,
            "key": identity.source_key,
            "uri": name,
        }


def uri_key(roots: list[Root], uri):
    """Lexical: the stored path may belong to a mount this process cannot see."""
    path = PurePosixPath(uri)
    for root in roots:
        for base in dict.fromkeys((root.base, root.given.parent if root.is_file() else root.given)):
            if path.is_relative_to(base.as_posix()) and path != PurePosixPath(base.as_posix()):
                return f"{root.label}:{path.relative_to(base.as_posix()).as_posix()}"
    return None


def stamp_existing(store, namespace, roots):
    """Name every feed staged before source keys existed. Idempotent; ids never change.

    Roots are the mounts the stored paths were written under. They need not exist
    here: ``claude=/sessions/claude`` stamps container paths from the host.
    """
    parsed = parse_roots(roots)
    rows = store.read(feed_rows, namespace)
    taken = {row["key"] for row in rows if row["key"]}
    counts = dict(feeds=len(rows), stamped=0, already_stamped=len(taken), unmatched=0, conflicts=0)
    stamps = []
    for row in rows:
        if row["key"]:
            continue
        key = uri_key(parsed, row["uri"]) if row["uri"] else None
        if key is None:
            counts["unmatched"] += 1
        elif key in taken:
            counts["conflicts"] += 1  # Two feeds claim one file; a person decides which.
        else:
            taken.add(key)
            stamps.append({"id": row["id"], "key": key, "uid": session_uid(Path(row["uri"]))})
    if stamps:
        counts["stamped"] = store.transaction(
            lambda tx: tx.run(
                "UNWIND $rows AS row MATCH (f:MemoryFeed {id:row.id,namespace:$ns}) "
                "WHERE f.source_key IS NULL "
                "SET f.source_key=row.key,f.session_uid=coalesce(row.uid,f.session_uid) "
                "RETURN count(f) AS n",
                rows=stamps,
                ns=namespace,
            ).single()["n"]
        )
    return counts
