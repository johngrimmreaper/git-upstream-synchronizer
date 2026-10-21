"""Adapter tests need the optional SDK but never contact a Camunda server."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

sdk = pytest.importorskip("camunda_orchestration_sdk")
from git_upstream_synchronizer import GitCommandError, SyncError
from git_upstream_synchronizer import camunda_worker as worker
from test_sync import repositories, commit_file, git


def job(repository_id="demo", retries=3):
    return SimpleNamespace(job_key="123", retries=retries,
                           variables=sdk.ActivatedJobResultVariables.from_dict({"repository_id": repository_id}))


def test_worker_calls_real_core(repositories):
    seed, fork = repositories
    config = {"demo": {"path": str(fork)}}
    commit_file(seed, "worker.txt", "from worker\n")
    git(seed, "push", "origin", "main")
    assert worker.handle_job(job(), config)["status"] == "UPDATED"
    assert worker.handle_job(job(), config)["status"] == "UP_TO_DATE"
    commit_file(fork, "local.txt", "preserve this\n")
    before = git(fork, "rev-parse", "HEAD")
    commit_file(seed, "diverged.txt", "upstream\n")
    git(seed, "push", "origin", "main")
    result = worker.handle_job(job(), config)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["current_sha"] == before


@pytest.mark.parametrize("repository_id", [None, [], "/etc", "unknown"])
def test_untrusted_process_path_is_rejected(repository_id):
    with pytest.raises(sdk.JobFailure) as caught:
        worker.handle_job(job(repository_id), {"demo": {"path": "/tmp/isolated"}})
    assert caught.value.retries == 0


def test_git_failure_requests_backoff_without_resetting_retries(monkeypatch):
    monkeypatch.setattr(worker, "synchronize_repository", Mock(side_effect=GitCommandError("offline")))
    with pytest.raises(sdk.JobFailure) as caught:
        worker.handle_job(job(retries=1), {"demo": {"path": "/tmp/isolated"}})
    assert caught.value.retries is None  # SDK decrements the current count, never resets to three.
    assert caught.value.retry_back_off == 5_000
    assert caught.value.message == "offline"


def test_unsafe_state_creates_incident(monkeypatch):
    monkeypatch.setattr(worker, "synchronize_repository", Mock(side_effect=SyncError("dirty")))
    with pytest.raises(sdk.JobFailure) as caught:
        worker.handle_job(job(), {"demo": {"path": "/tmp/isolated"}})
    assert caught.value.retries == 0


def test_unexpected_failure_is_not_converted_to_success(monkeypatch):
    monkeypatch.setattr(worker, "synchronize_repository", Mock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        worker.handle_job(job(), {"demo": {"path": "/tmp/isolated"}})


@pytest.mark.parametrize("data", [[], {}, {"x": {}}, {"x": {"path": "relative"}},
                                 {"x": {"path": "/tmp/x", "upstream": []}},
                                 {"x": {"path": "/tmp/x", "command": "arbitrary"}}])
def test_invalid_allowlist(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        worker.load_repositories(path)


def test_valid_allowlist(tmp_path):
    path = tmp_path / "config.json"
    data = {"demo": {"path": "/tmp/isolated", "branch": "main", "upstream": "upstream"}}
    path.write_text(json.dumps(data))
    assert worker.load_repositories(path) == data



def test_git_write_lock_creates_incident_without_retry(repositories):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    lock = fork / ".git" / "index.lock"
    lock.write_bytes(b"unfinished write\n")
    with pytest.raises(sdk.JobFailure) as caught:
        worker.handle_job(job(), {"demo": {"path": str(fork)}})
    assert caught.value.retries == 0
    assert "Git lock file: index.lock" in caught.value.message
    assert lock.read_bytes() == b"unfinished write\n"
    assert git(fork, "rev-parse", "HEAD") == before
