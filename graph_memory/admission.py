"""Bound historical MCP working sets across HTTP threads and stdio processes."""

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class HistoryBusy(Exception):
    pass


@contextmanager
def historical_read():
    # All namespaces share container RAM. Never unlink: a replacement inode
    # would permit another process to hold a different lock on the same path.
    path = Path(tempfile.gettempdir()) / f"graph-memory-history-{os.getuid()}.lock"
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HistoryBusy from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
