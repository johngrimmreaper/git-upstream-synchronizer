"""Safety regressions exercise real Git without any orchestration backend."""

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
from git_upstream_synchronizer import GitCommandError, SyncError, synchronize_repository
from test_sync import repositories, commit_file, git


@pytest.mark.parametrize("options", [{"branch": "--help"}, {"branch": "bad:name"},
                                   {"upstream": "--upload-pack=evil"}, {"upstream": "/tmp/other.git"}])
def test_invalid_configuration(repositories, options):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    with pytest.raises(SyncError):
        synchronize_repository(fork, **options)
    assert git(fork, "rev-parse", "HEAD") == before


def test_detached_head(repositories):
    _, fork = repositories
    git(fork, "switch", "--detach")
    with pytest.raises(SyncError, match="Expected checked-out branch"):
        synchronize_repository(fork)


def test_untracked_files_cannot_be_hidden_by_configuration(repositories):
    _, fork = repositories
    git(fork, "config", "status.showUntrackedFiles", "no")
    (fork / "hidden.txt").write_text("keep me")
    with pytest.raises(SyncError, match="uncommitted or untracked"):
        synchronize_repository(fork)


def test_unfinished_merge(repositories):
    _, fork = repositories
    (fork / ".git" / "MERGE_HEAD").write_text(git(fork, "rev-parse", "HEAD") + "\n")
    with pytest.raises(SyncError, match="unfinished Git operation"):
        synchronize_repository(fork)


def test_concurrent_sync_is_refused(repositories):
    _, fork = repositories
    with (fork / ".git" / "upstream-synchronizer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(GitCommandError, match="Another synchronization"):
            synchronize_repository(fork)
    assert synchronize_repository(fork).status == "UP_TO_DATE"


def test_failed_fetch_preserves_head(repositories):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    git(fork, "remote", "set-url", "upstream", str(fork / "missing.git"))
    with pytest.raises(GitCommandError, match="fetch failed"):
        synchronize_repository(fork)
    assert git(fork, "rev-parse", "HEAD") == before


def test_ignored_file_is_not_overwritten(repositories):
    seed, fork = repositories
    (fork / ".git" / "info" / "exclude").write_text("precious.txt\n")
    (fork / "precious.txt").write_text("local ignored content\n")
    commit_file(seed, "precious.txt", "upstream content\n")
    git(seed, "push", "origin", "main")
    before = git(fork, "rev-parse", "HEAD")
    with pytest.raises(GitCommandError):
        synchronize_repository(fork)
    assert git(fork, "rev-parse", "HEAD") == before
    assert (fork / "precious.txt").read_text() == "local ignored content\n"


@pytest.mark.parametrize("phase", ["fetch", "post-merge"])
@pytest.mark.parametrize("kill_scope", ["group", "parent"])
def test_killed_sync_keeps_lock_until_git_exits(repositories, phase, kill_scope):
    seed, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    commit_file(seed, "after-crash.txt", "upstream change\n")
    git(seed, "push", "origin", "main")
    upstream = git(seed, "rev-parse", "HEAD")
    blocker = fork / ".git" / ("test-upload-pack" if phase == "fetch" else "hooks/post-merge")
    marker = Path(str(blocker) + ".reached")
    release = Path(str(blocker) + ".release")
    tail = 'exec git-upload-pack "$@"\n' if phase == "fetch" else "exit 0\n"
    blocker.write_text('#!/bin/sh\n: > "$0.reached"\n'
                       'while [ ! -e "$0.release" ]; do sleep 0.02; done\n' + tail)
    blocker.chmod(0o755)
    if phase == "fetch":
        git(fork, "config", "remote.upstream.uploadpack", str(blocker))
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
            assert process.poll() is None, process.communicate(timeout=1)[1].decode()
            assert time.monotonic() < deadline, f"Git did not reach {phase}"
            time.sleep(0.02)
        expected_sha = before if phase == "fetch" else upstream
        assert git(fork, "rev-parse", "HEAD") == expected_sha
        with pytest.raises(GitCommandError, match="Another synchronization"):
            synchronize_repository(fork)
        if kill_scope == "group":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()  # The blocked Git child remains alive.
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
        assert git(fork, "rev-parse", "HEAD") == expected_sha
        if kill_scope == "parent":
            with (fork / ".git" / "upstream-synchronizer.lock").open("a") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(GitCommandError, match="Another synchronization"):
                synchronize_repository(fork)
            release.touch()
            deadline = time.monotonic() + 5
            while True:
                try:
                    with (fork / ".git" / "upstream-synchronizer.lock").open("a") as probe:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline, "Orphan Git did not release its lock"
                    time.sleep(0.02)
    finally:
        release.touch()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.communicate(timeout=5)
        blocker.unlink()
        if phase == "fetch":
            git(fork, "config", "--unset", "remote.upstream.uploadpack")
    assert (fork / ".git" / "upstream-synchronizer.lock").exists()
    result = synchronize_repository(fork)
    assert result.status == ("UPDATED" if phase == "fetch" else "UP_TO_DATE")
    assert result.current_sha == upstream
    assert (fork / "after-crash.txt").read_text() == "upstream change\n"


@pytest.mark.parametrize("lock_name", ["index.lock", "HEAD.lock", "refs/heads/main.lock", "packed-refs.lock"])
def test_git_lock_is_preserved_and_refused_before_fetch(repositories, lock_name):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    lock = Path(git(fork, "rev-parse", "--path-format=absolute", "--git-path", lock_name))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"unfinished Git write\n")
    fetch_head = fork / ".git" / "FETCH_HEAD"
    previous_fetch = fetch_head.read_bytes() if fetch_head.exists() else None
    with pytest.raises(SyncError, match="Git lock file"):
        synchronize_repository(fork)
    assert lock.read_bytes() == b"unfinished Git write\n"
    assert git(fork, "rev-parse", "HEAD") == before
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == previous_fetch


def test_partial_worktree_write_is_preserved_and_refused_before_fetch(repositories):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    tracked = fork / "initial.txt"
    tracked.write_bytes(b"partial\x00write")
    fetch_head = fork / ".git" / "FETCH_HEAD"
    previous_fetch = fetch_head.read_bytes() if fetch_head.exists() else None
    with pytest.raises(SyncError, match="uncommitted or untracked"):
        synchronize_repository(fork)
    assert tracked.read_bytes() == b"partial\x00write"
    assert git(fork, "rev-parse", "HEAD") == before
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == previous_fetch


def test_corrupt_index_is_preserved_and_refused_before_fetch(repositories):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    index = fork / ".git" / "index"
    index.write_bytes(b"truncated index")
    fetch_head = fork / ".git" / "FETCH_HEAD"
    previous_fetch = fetch_head.read_bytes() if fetch_head.exists() else None
    with pytest.raises(GitCommandError, match="status failed"):
        synchronize_repository(fork)
    assert index.read_bytes() == b"truncated index"
    assert git(fork, "rev-parse", "HEAD") == before
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == previous_fetch
