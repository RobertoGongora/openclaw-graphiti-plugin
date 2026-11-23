"""The feed permission canary, in its disposable container. Opt-in: it needs
Docker and builds a small image. MEMORY_TEST_FEED_CANARY=1 runs it."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUN = Path(__file__).parents[1] / "evals" / "feed_permissions" / "run.sh"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("MEMORY_TEST_FEED_CANARY") and shutil.which("docker")),
    reason="Set MEMORY_TEST_FEED_CANARY=1 with Docker available",
)


def canary(fs="volume", **env):
    result = subprocess.run(
        [str(RUN)],
        env={**os.environ, "FS": fs, **env},
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout


@pytest.mark.parametrize("fs", ["volume", "tmpfs"])
def test_least_privilege_inbox_passes(fs):
    code, out = canary(fs)
    assert code == 0, out
    assert "CANARY PASS" in out and "FAIL" not in out
    assert "PASS N1 plain rsync -a of 0600 files leaves the reader out (denied)" in out


def test_plain_rsync_fails_the_canary():
    code, out = canary(CANARY_RSYNC="rsync -a")
    assert code == 1
    assert "FAIL reader reads a session\n" in out
    assert "FAIL reader reads the renamed session\n" in out


WRITABLE = "u:10001:rwx,d:u::rwx,d:u:10001:rwx,d:g::-,d:m::rwx,d:o::-"


def test_writable_reader_grant_fails_the_canary():
    # The mask from --chmod still blocks the write; the ACL check reports the grant.
    code, out = canary(CANARY_ACL=WRITABLE)
    assert code == 1
    assert "PASS reader creates a file (denied)" in out
    assert "FAIL the reader is never granted write" in out
    code, out = canary(CANARY_ACL=WRITABLE, CANARY_RSYNC="rsync -a --chmod=D770,F660")
    assert code == 1
    assert "FAIL reader appends to a session (allowed)" in out
