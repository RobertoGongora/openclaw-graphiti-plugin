"""Operational limits, read once from the environment with bounds."""

import os


def _number(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def intake_queue():
    """Episodes due before intake leaves further source on disk."""
    return _number("MEMORY_INTAKE_QUEUE", 32, 1, 10_000)


def intake_files():
    """Files with work left that one scan may open."""
    return _number("MEMORY_INTAKE_FILES", 4, 1, 1_000)


def llm_timeout():
    """Seconds for one model call. The longest seen in production was 356."""
    return _number("MEMORY_LLM_TIMEOUT", 420, 10, 3_600)


def lease_seconds(llm):
    """An episode can take two extraction passes of two model calls each."""
    return max(900, getattr(llm, "timeout", llm_timeout()) * 4 + 60)


def summary():
    return {
        "intake_queue": intake_queue(),
        "intake_files": intake_files(),
        "llm_timeout": llm_timeout(),
    }
