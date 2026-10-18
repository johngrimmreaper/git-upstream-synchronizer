"""Integration tests use isolated local Git repositories, never GitHub."""

from pathlib import Path
import subprocess
import pytest

from git_upstream_synchronizer import SyncError, synchronize_repository


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, text=True, capture_output=True, timeout=15,
    )
    return result.stdout.strip()


def commit_file(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", f"Add {name}")


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream.git"
    seed = tmp_path / "seed"
    fork = tmp_path / "fork"

    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(upstream), str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.name", "Test Author")
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "switch", "-c", "main")
    commit_file(seed, "initial.txt", "initial\n")
    git(seed, "push", "origin", "main")

    subprocess.run(
        ["git", "clone", "-b", "main", str(upstream), str(fork)],
        check=True, capture_output=True,
    )
    git(fork, "config", "user.name", "Test Author")
    git(fork, "config", "user.email", "test@example.invalid")
    git(fork, "remote", "rename", "origin", "upstream")
    return seed, fork


def test_fast_forward_and_idempotency(repositories: tuple[Path, Path]) -> None:
    seed, fork = repositories
    commit_file(seed, "new.txt", "new\n")
    git(seed, "push", "origin", "main")

    first = synchronize_repository(fork)
    assert first.status == "UPDATED"
    assert first.previous_sha != first.current_sha
    assert first.upstream_sha == first.current_sha
    assert (fork / "new.txt").read_text() == "new\n"

    second = synchronize_repository(fork)
    assert second.status == "UP_TO_DATE"
    assert second.current_sha == first.current_sha


def test_diverged_history_does_not_mutate_branch(repositories: tuple[Path, Path]) -> None:
    seed, fork = repositories
    commit_file(fork, "local.txt", "fork changes\n")
    local_sha = git(fork, "rev-parse", "HEAD")
    commit_file(seed, "remote.txt", "upstream changes\n")
    git(seed, "push", "origin", "main")

    result = synchronize_repository(fork)
    assert result.status == "NEEDS_REVIEW"
    assert git(fork, "rev-parse", "HEAD") == local_sha
    assert not (fork / "remote.txt").exists()


def test_dirty_worktree_is_rejected(repositories: tuple[Path, Path]) -> None:
    _, fork = repositories
    (fork / "untracked.txt").write_text("do not touch\n")
    with pytest.raises(SyncError, match="uncommitted or untracked"):
        synchronize_repository(fork)


def test_wrong_branch_is_rejected(repositories: tuple[Path, Path]) -> None:
    _, fork = repositories
    with pytest.raises(SyncError, match="Expected checked-out branch"):
        synchronize_repository(fork, branch="release")
