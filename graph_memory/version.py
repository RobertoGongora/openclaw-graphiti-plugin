"""Content-addressed engine identity: what decides the facts an episode yields."""

import functools
import hashlib
import importlib.metadata
from pathlib import Path

# Cached extractions, quarantines and dreams are only as good as the code that
# produced them, so they are keyed by this. Operational code is left out on
# purpose: a change to the CLI or the status tool must not send every
# quarantined episode back to the model or stale every dream.
ENGINE_FILES = (
    "models.py",
    "llm.py",
    "extraction_policy.py",
    "recall_provenance.py",
    "retry.py",
    "diagnostics.py",
    "service.py",
    "temporal.py",
    "session_sources.py",
    "importers.py",
    "source_graph.py",
    "store.py",
)


def engine_fingerprint(fresh=False):
    """fresh re-reads the files: how a long-lived process notices they changed."""
    return _read() if fresh else _cached()


def _read():
    digest = hashlib.sha256()
    source = Path(__file__).parent
    for name in ENGINE_FILES:
        digest.update(name.encode())
        digest.update((source / name).read_bytes())
    for dependency in ("neo4j", "pydantic"):
        # Major.minor only: a patch release does not change what is extracted.
        version = ".".join(importlib.metadata.version(dependency).split(".")[:2])
        digest.update(f"{dependency}:{version}".encode())
    return digest.hexdigest()


_cached = functools.cache(_read)
