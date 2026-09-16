"""Content-addressed engine identity, including extraction/dream prompts and schemas."""

import hashlib
import importlib.metadata
from pathlib import Path


def engine_fingerprint():
    digest = hashlib.sha256()
    source = Path(__file__).parent
    for path in sorted([*source.glob("*.py"), *(source.parent / "evals").glob("*.py")]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for dependency in ("neo4j", "pydantic"):
        digest.update(f"{dependency}:{importlib.metadata.version(dependency)}".encode())
    return digest.hexdigest()
