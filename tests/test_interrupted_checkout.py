"""Interrupted Git writes require inspection, never automatic lock removal."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from git_upstream_synchronizer import SyncError, synchronize_repository
from test_sync import commit_file, git, repositories


@pytest.mark.parametrize("marker", [
    "index.lock", "HEAD.lock", "refs/heads/main.lock", "packed-refs.lock",
])
def test_git_lock_in_linked_worktree_refuses_before_fetch(repositories, marker):
    _, fork = repositories
    git(fork, "switch", "--detach")
    checkout = fork.parent / "linked"
    git(fork, "worktree", "add", str(checkout), "main")
    fork = checkout
    before = git(fork, "rev-parse", "HEAD")
    lock = fork / git(fork, "rev-parse", "--git-path", marker)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"preserve this Git lock\n")
    fetch_head = fork / git(fork, "rev-parse", "--git-path", "FETCH_HEAD")
    fetched_before = fetch_head.read_bytes() if fetch_head.exists() else None

    with pytest.raises(SyncError, match="Git lock"):
        synchronize_repository(fork)

    assert git(fork, "rev-parse", "HEAD") == before
    assert lock.read_bytes() == b"preserve this Git lock\n"
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == fetched_before


def test_killed_checkout_preserves_partial_files_and_git_lock(repositories):
    seed, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    commit_file(seed, "initial.txt", "changed before interruption\n")
    commit_file(seed, "z-blocked.txt", "content awaiting checkout\n")
    git(seed, "push", "origin", "main")
    upstream = git(seed, "rev-parse", "HEAD")
    blocker = fork / ".git" / "test-smudge"
    marker = Path(str(blocker) + ".reached")
    blocker.write_text('#!/bin/sh\n: > "$0.reached"\n'
                       'while [ ! -e "$0.release" ]; do sleep 0.02; done\ncat\n')
    blocker.chmod(0o755)
    (fork / ".git" / "info" / "attributes").write_text("z-blocked.txt filter=interrupted\n")
    git(fork, "config", "filter.interrupted.smudge", str(blocker))
    git(fork, "config", "filter.interrupted.clean", "cat")
    git(fork, "config", "filter.interrupted.required", "true")

    from git_upstream_synchronizer import sync
    env = {**os.environ, "PYTHONPATH": str(Path(sync.__file__).resolve().parents[1])}
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from git_upstream_synchronizer import synchronize_repository; "
         "synchronize_repository(Path(sys.argv[1]))", str(fork)],
        env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None, "Synchronizer exited before reaching checkout"
            assert time.monotonic() < deadline, "Git did not reach the smudge filter"
            time.sleep(0.02)
        # Git has written one file but has not updated the index or branch yet.
        assert (fork / "initial.txt").read_text() == "changed before interruption\n"
        assert git(fork, "rev-parse", "HEAD") == before
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.communicate(timeout=5)

    lock = fork / ".git" / "index.lock"
    assert lock.exists()
    files_before = {path.name: path.read_bytes() for path in fork.iterdir() if path.is_file()}
    lock_before = lock.read_bytes()
    with pytest.raises(SyncError, match="Git lock"):
        synchronize_repository(fork)
    assert git(fork, "rev-parse", "HEAD") == before
    assert git(fork, "rev-parse", "FETCH_HEAD") == upstream
    assert git(fork, "show", "HEAD:initial.txt") == "initial"
    assert {path.name: path.read_bytes() for path in fork.iterdir() if path.is_file()} == files_before
    assert lock.read_bytes() == lock_before
