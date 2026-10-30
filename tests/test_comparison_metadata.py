"""Regression checks for exact tip comparisons and unusual commit metadata."""
import pytest
from git_upstream_synchronizer import comparison
from git_upstream_synchronizer.sync import GitCommandError
from test_sync import commit_file, git, repositories


def test_commit_metadata_line_separators_and_rename_tip_diff(repositories):
    seed, fork = repositories
    commit_file(seed, "old.txt", "rename me\n")
    git(seed, "push", "origin", "main")
    from git_upstream_synchronizer.sync import synchronize_repository
    synchronize_repository(fork)
    git(seed, "mv", "old.txt", "new.txt")
    subject = "Literal <script>subject</script>\u0085still the same subject"
    git(seed, "-c", "commit.gpgsign=false", "commit", "-m", subject)
    git(seed, "push", "origin", "main")
    commit_file(fork, "local-only.txt", "keep local\n")
    result = comparison.compare_repository(fork)
    assert result["status"] == "DIVERGED"
    assert result["upstream_commits"][0]["subject"] == subject
    changes = {item["path"]: item for item in result["files"]}
    assert changes["old.txt"]["deleted"] == "1"
    assert changes["new.txt"]["added"] == "1"
    # A tip-to-tip preview includes removal of local-only content. A merge-base
    # preview would omit it; no checkout content is actually removed.
    assert changes["local-only.txt"]["deleted"] == "1"
    assert (fork / "local-only.txt").read_text() == "keep local\n"


def test_oversized_commit_metadata_is_not_silently_omitted(repositories, monkeypatch):
    _, fork = repositories
    monkeypatch.setattr(comparison, "INVENTORY_LIMIT", 1)
    with pytest.raises(GitCommandError, match="Commit metadata exceeds"):
        comparison._commits(fork, "HEAD")
