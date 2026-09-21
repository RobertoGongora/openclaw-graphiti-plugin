"""Transcript feed identity that survives a change of mount point or host path.

A file is named by its source key, ``LABEL:relative/posix/path.jsonl``: the path
below the transcript root it was found in, prefixed by a label for that root.
The label comes from the root's name, skipping the generic directory names the
hosts use, so ``/sessions/claude`` and ``~/.claude/projects`` are both
``claude``. A root given as ``LABEL=PATH`` states its label instead. A root whose
every component is generic or has no letters (``/``, ``/sessions``) is labelled
``root``: state a label for it, since two such roots clash.

A change of roots must never mint a feed for a file already known. A feed is found
by key, then by the path it was last stored with; a file that is new by both but
carries the name of a known feed is refused, and nothing at all is staged while
older feeds cannot be named under the current roots (``blocked``).

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
LABEL = re.compile(r"[\w.-]+")
UID_LINES, UID_BYTES = 64, 262_144
# Session uuids, rollout names and agent hashes name one file wherever it sits.
UNIQUE_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-|[0-9a-f]{12,}")
ACCEPT_UNMATCHED = "MEMORY_FEED_ACCEPT_UNMATCHED"


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
    """The nearest component, from the root's own name upwards, that names a host."""
    for name in reversed(directory.parts):
        name = name.lstrip(".")
        if name.lower() not in GENERIC and re.search(r"\w", name):
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


def validate_roots(roots) -> list[Root]:
    """Parse the roots, most specific first so overlapping roots name a file the
    same in any order. Two directories under one label would merge their files."""
    owners = {}
    parsed = []
    for given in roots:
        root = parse_root(given)
        first, base = owners.setdefault(root.label, (given, root.base))
        if base != root.base:
            raise ValueError(
                f"Transcript roots {str(first)!r} and {str(given)!r} share the label "
                f"{root.label!r}; give one of them as LABEL=PATH"
            )
        parsed.append(root)
    return sorted(parsed, key=lambda root: -len(root.base.parts))


def source_files(roots) -> dict[str, str]:
    """Resolved path -> source key for every transcript below the roots."""
    found = {}
    for root in validate_roots(roots):
        files = [root.given] if root.is_file() else root.given.rglob("*.jsonl")
        for file in files:
            name, key = root.key(file)
            found.setdefault(name, key)
    return found


def session_uid(path: Path):
    """The session's own id as its records state it. Informational: subagent
    transcripts repeat their parent's id, so it cannot name a file."""
    try:
        with path.open(errors="replace") as stream:
            # Bounded: this runs for every older feed before the first scan.
            for line in islice(stream.read(UID_BYTES).splitlines(), UID_LINES):
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


class KnownElsewhere(ValueError):
    """A file that is new by key and by path, under the name of a known feed."""

    def __init__(self, row):
        super().__init__("A feed for a file of this name exists under another key")
        self.feed_id, self.source_key = row["id"], row["key"]


def feed_rows(tx, namespace):
    return tx.run(
        "MATCH (f:MemoryFeed {namespace:$ns,source_format:$format}) "
        "RETURN f.id AS id,f.session_id AS session,f.source_key AS key,f.source_uri AS uri",
        ns=namespace,
        format=FORMAT,
    ).data()


def file_name(uri):
    """What names this file wherever its tree is mounted: a unique basename, or
    the basename with its directory (every workflow has a journal.jsonl)."""
    path = PurePosixPath(uri)
    return path.name if UNIQUE_NAME.search(path.name) else "/".join(path.parts[-2:])


def accept_unmatched():
    return os.environ.get(ACCEPT_UNMATCHED) == "1"


class Feeds:
    """The namespace's feed cursors: by source key, by stored path, by file name."""

    def __init__(self, store, namespace, roots=None):
        self.namespace = namespace
        self.by_key, self.by_uri, self.by_name = {}, {}, {}
        rows = store.read(feed_rows, namespace)
        for row in rows:
            if row["key"]:
                self.by_key[row["key"]] = row
            if row["uri"]:
                held = self.by_uri.get(row["uri"])
                # The watcher's own feed for a path wins over one a direct caller made.
                if not held or row["session"] == "host:" + digest(row["uri"]):
                    self.by_uri[row["uri"]] = row
                self.by_name.setdefault(file_name(row["uri"]), row)
        # Older feeds these roots cannot name: their files would all read as new.
        self.blocked = {}
        if roots is not None and not accept_unmatched():
            counts = plan(rows, validate_roots(roots))[1]
            if counts["unmatched"] or counts["conflicts"]:
                self.blocked = counts

    def resolve(self, key, name) -> Identity:
        row = self.by_key.get(key) or self.by_uri.get(name)
        if row:
            return Identity(row["id"], row["session"], key, True)
        row = self.by_name.get(file_name(name))
        if row and not accept_unmatched():
            raise KnownElsewhere(row)
        session = "source:" + digest(key)
        return Identity(digest([FORMAT, self.namespace, key, session]), session, key, False)

    def record(self, identity: Identity, name):
        row = {
            "id": identity.feed_id,
            "session": identity.session_id,
            "key": identity.source_key,
            "uri": name,
        }
        for index in (self.by_key, self.by_uri):
            for held in [k for k, v in index.items() if v["id"] == identity.feed_id]:
                del index[held]
        self.by_key[identity.source_key] = self.by_uri[name] = row
        self.by_name[file_name(name)] = row

    def rekey(self, store, files):
        """Name by today's roots every feed found by its path alone. A fully fed
        file is never opened again, so this cannot wait for feed_records: the key
        it kept would not find the feed after the next move."""
        changed = []
        for name, key in files.items():
            row = self.by_uri.get(name)
            if row and row["key"] != key and key not in self.by_key:
                changed.append((Identity(row["id"], row["session"], key, True), name))
        if changed:
            store.transaction(
                lambda tx: tx.run(
                    "UNWIND $rows AS row MATCH (f:MemoryFeed {id:row.id,namespace:$ns}) "
                    "SET f.source_key=row.key",
                    rows=[{"id": i.feed_id, "key": i.source_key} for i, _ in changed],
                    ns=self.namespace,
                ).consume()
            )
            for identity, name in changed:
                self.record(identity, name)
        return len(changed)


def uri_key(roots: list[Root], uri):
    """Lexical: the stored path may belong to a mount this process cannot see."""
    path = PurePosixPath(uri)
    for root in roots:
        for base in dict.fromkeys((root.base, root.given.parent if root.is_file() else root.given)):
            if path.is_relative_to(base.as_posix()) and path != PurePosixPath(base.as_posix()):
                return f"{root.label}:{path.relative_to(base.as_posix()).as_posix()}"
    return None


def plan(rows, roots: list[Root]):
    """Which unnamed feeds the roots can name, and the counts stamping would report."""
    taken = {row["key"] for row in rows if row["key"]}
    counts = dict(feeds=len(rows), stamped=0, already_stamped=len(taken), unmatched=0, conflicts=0)
    stamps = []
    for row in rows:
        if row["key"]:
            continue
        key = uri_key(roots, row["uri"]) if row["uri"] else None
        if key is None:
            counts["unmatched"] += 1
        elif key in taken:
            counts["conflicts"] += 1  # Two feeds claim one file; a person decides which.
        else:
            taken.add(key)
            stamps.append({"id": row["id"], "key": key, "uri": row["uri"]})
    return stamps, counts


def stamp_existing(store, namespace, roots) -> dict[str, int]:
    """Name every feed staged before source keys existed. Idempotent; ids never change.

    Roots are paths or ``LABEL=PATH`` strings, PATH being the prefix the stored
    paths were written under. Matching is lexical, so PATH need not exist here:
    ``claude=/sessions/claude`` stamps container paths from the host.
    Returns feeds, stamped, already_stamped, unmatched, conflicts.
    """
    stamps, counts = plan(store.read(feed_rows, namespace), validate_roots(roots))
    if stamps:
        # Files are read before the transaction, never under the namespace lock.
        rows = [{**stamp, "uid": session_uid(Path(stamp["uri"]))} for stamp in stamps]
        counts["stamped"] = store.transaction(
            lambda tx: tx.run(
                "UNWIND $rows AS row MATCH (f:MemoryFeed {id:row.id,namespace:$ns}) "
                "WHERE f.source_key IS NULL "
                "SET f.source_key=row.key,f.session_uid=coalesce(row.uid,f.session_uid) "
                "RETURN count(f) AS n",
                rows=rows,
                ns=namespace,
            ).single()["n"]
        )
    return counts
