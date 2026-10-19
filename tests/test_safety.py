"""Safety regressions exercise real Git without any orchestration backend."""

import fcntl
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
