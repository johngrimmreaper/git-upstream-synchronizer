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
def test_killed_sync_releases_lock_and_can_be_repeated(repositories, phase):
    seed, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    commit_file(seed, "after-crash.txt", "upstream change\n")
    git(seed, "push", "origin", "main")
    upstream = git(seed, "rev-parse", "HEAD")
    blocker = fork / ".git" / ("test-upload-pack" if phase == "fetch" else "hooks/post-merge")
    marker = Path(str(blocker) + ".reached")
    blocker.write_text('#!/bin/sh\n: > "$0.reached"\nexec sleep 60\n')
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
        # Kill the child Git/hook processes as well as their Python parent.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
        assert git(fork, "rev-parse", "HEAD") == expected_sha
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
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
