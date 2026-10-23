"""Inherited Git context must not redirect synchronization to another checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from git_upstream_synchronizer import SyncError, synchronize_repository
from test_sync import commit_file, git, repositories


@pytest.fixture
def competing_checkouts(repositories, tmp_path):
    seed, requested = repositories
    other = tmp_path / "other"
    git(requested, "clone", "--branch", "main", str(seed.parent / "upstream.git"), str(other))
    git(other, "remote", "rename", "origin", "upstream")
    commit_file(seed, "incoming.txt", "upstream change\n")
    git(seed, "push", "origin", "main")
    return requested, other


def state(repo):
    fetch_head = repo / ".git" / "FETCH_HEAD"
    return (
        git(repo, "rev-parse", "HEAD"),
        (repo / ".git" / "index").read_bytes(),
        fetch_head.read_bytes() if fetch_head.exists() else None,
        {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()},
    )


@pytest.mark.parametrize("mode", ["repository", "index", "objects", "inline-config", "common", "shallow"])
def test_repository_local_context_is_refused(competing_checkouts, tmp_path, monkeypatch, mode):
    requested, other = competing_checkouts
    outside = tmp_path / "outside-metadata"
    outside.write_bytes(b"preserve external metadata\n")
    overrides = {
        "repository": {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)},
        "index": {"GIT_INDEX_FILE": str(outside)},
        "objects": {"GIT_OBJECT_DIRECTORY": str(tmp_path / "outside-objects")},
        "inline-config": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                          "GIT_CONFIG_VALUE_0": str(other)},
        "common": {"GIT_COMMON_DIR": str(other / ".git")},
        "shallow": {"GIT_SHALLOW_FILE": str(outside)},
    }[mode]
    before = state(requested), state(other)
    with monkeypatch.context() as context:
        for name, value in overrides.items():
            context.setenv(name, value)
        with pytest.raises(SyncError, match="repository-local Git environment"):
            synchronize_repository(requested)
    assert (state(requested), state(other)) == before
    assert outside.read_bytes() == b"preserve external metadata\n"
    assert not (requested / ".git" / "upstream-synchronizer.lock").exists()
    assert not (other / ".git" / "upstream-synchronizer.lock").exists()


def test_cli_refuses_repository_redirection(competing_checkouts):
    requested, other = competing_checkouts
    before = state(requested), state(other)
    result = subprocess.run(
        [sys.executable, "-m", "git_upstream_synchronizer.cli", str(requested)],
        env={**os.environ, "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    assert not result.stdout
    error = json.loads(result.stderr)["error"]
    assert "GIT_DIR" in error and "GIT_WORK_TREE" in error
    assert str(other) not in error
    assert (state(requested), state(other)) == before


def test_ordinary_git_settings_remain_supported(competing_checkouts, monkeypatch):
    requested, other = competing_checkouts
    before_other = state(other)
    with monkeypatch.context() as context:
        context.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        context.setenv("GIT_CONFIG_NOSYSTEM", "1")
        context.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        result = synchronize_repository(requested)
    assert result.status == "UPDATED"
    assert (requested / "incoming.txt").read_text() == "upstream change\n"
    assert state(other) == before_other
