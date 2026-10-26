"""Real Git and HTTP checks for the SDK-free local web interface."""

from contextlib import contextmanager
import fcntl
import http.client
import json
import threading

import pytest

from git_upstream_synchronizer import SyncError
from git_upstream_synchronizer import comparison
from git_upstream_synchronizer.web import WebServer
from test_sync import commit_file, git, repositories


@pytest.mark.parametrize(("local", "remote", "status"), [
    (False, False, "UP_TO_DATE"), (False, True, "BEHIND"),
    (True, False, "AHEAD"), (True, True, "DIVERGED"),
])
def test_preview_classifies_without_updating_checkout(repositories, local, remote, status):
    seed, fork = repositories
    if local:
        commit_file(fork, "local.txt", "keep local\n")
    if remote:
        commit_file(seed, "incoming.txt", "incoming\n")
        git(seed, "push", "origin", "main")
    before = git(fork, "rev-parse", "HEAD")
    result = comparison.compare_repository(fork)
    assert result["status"] == status
    assert (result["ahead"], result["behind"]) == (int(local), int(remote))
    assert len(result["local_commits"]) == int(local)
    assert len(result["upstream_commits"]) == int(remote)
    assert result["local_sha"] == before == git(fork, "rev-parse", "HEAD")
    assert not (fork / "incoming.txt").exists()


def test_unrelated_history_is_reported(repositories):
    seed, fork = repositories
    git(seed, "switch", "--orphan", "replacement")
    commit_file(seed, "replacement.txt", "unrelated root\n")
    git(seed, "push", "--force", "origin", "HEAD:main")
    before = git(fork, "rev-parse", "HEAD")
    assert comparison.compare_repository(fork)["status"] == "UNRELATED"
    assert git(fork, "rev-parse", "HEAD") == before


def test_preview_handles_literal_filenames_and_binary_files(repositories):
    seed, fork = repositories
    name = "odd[1]*\tline\n<script>.txt"
    commit_file(seed, name, "<img src=x onerror=alert(1)>\n")
    (seed / "binary.bin").write_bytes(b"\x00\xff\x00")
    git(seed, "add", "binary.bin")
    git(seed, "-c", "commit.gpgsign=false", "commit", "-m", "Binary update")
    git(seed, "push", "origin", "main")
    git(fork, "config", "diff.external", "/not-an-external-diff")
    result = comparison.compare_repository(fork)
    changes = {file["path"]: file for file in result["files"]}
    assert "+<img src=x onerror=alert(1)>" in changes[name]["patch"]
    assert changes["binary.bin"]["binary"]
    assert changes["binary.bin"]["patch"] == ""


def test_preview_reports_complete_counts_with_capped_details(repositories, monkeypatch):
    seed, fork = repositories
    for name in ("one.txt", "two.txt"):
        commit_file(seed, name, "long line\n" * 100)
    git(seed, "push", "origin", "main")
    monkeypatch.setattr(comparison, "COMMIT_LIMIT", 1)
    monkeypatch.setattr(comparison, "FILE_LIMIT", 1)
    monkeypatch.setattr(comparison, "PATCH_LIMIT", 80)
    result = comparison.compare_repository(fork)
    assert result["behind"] == result["total_files"] == 2
    assert len(result["upstream_commits"]) == len(result["files"]) == 1
    assert len(result["files"][0]["patch"]) == 80
    assert result["files"][0]["truncated"]


@contextmanager
def running_server(fork):
    server = WebServer({"demo": {"path": str(fork)}}, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(route, data=None, headers=None, method=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        defaults = {"Content-Type": "application/json", "X-Sync-Token": server.token}
        defaults.update(headers or {})
        body = json.dumps(data) if data is not None else None
        connection.request(method or ("POST" if data is not None else "GET"),
                           route, body=body, headers=defaults)
        response = connection.getresponse()
        raw = response.read().decode()
        result = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
        status = response.status
        connection.close()
        return status, result

    try:
        yield server, request
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_page_assets_and_real_sync(repositories):
    seed, fork = repositories
    commit_file(seed, "web.txt", "updated from browser\n")
    git(seed, "push", "origin", "main")
    with running_server(fork) as (server, request):
        assert server.server_address[0] == "127.0.0.1"
        status, html = request("/")
        assert status == 200 and server.token in html and "__REQUEST_TOKEN__" not in html
        assert request("/app.js")[0] == request("/app.css")[0] == 200
        assert request("/api/repositories")[1] == {
            "repositories": [{"id": "demo", "branch": "main", "upstream": "upstream"}],
        }
        assert request("/api/compare", {"repository_id": "demo"})[1]["status"] == "BEHIND"
        assert not (fork / "web.txt").exists()
        assert request("/api/sync", {"repository_id": "demo"})[1]["status"] == "UPDATED"
        assert (fork / "web.txt").read_text() == "updated from browser\n"
        assert request("/api/compare", {"repository_id": "demo"})[1]["status"] == "UP_TO_DATE"
        assert request("/api/sync", {"repository_id": "demo"})[1]["status"] == "UP_TO_DATE"


def test_sync_rechecks_a_stale_preview(repositories):
    seed, fork = repositories
    commit_file(seed, "incoming.txt", "upstream\n")
    git(seed, "push", "origin", "main")
    with running_server(fork) as (_, request):
        assert request("/api/compare", {"repository_id": "demo"})[1]["status"] == "BEHIND"
        commit_file(fork, "local.txt", "preserve\n")
        before = git(fork, "rev-parse", "HEAD")
        assert request("/api/sync", {"repository_id": "demo"})[1]["status"] == "NEEDS_REVIEW"
        assert git(fork, "rev-parse", "HEAD") == before
        (fork / "dirty.txt").write_text("keep\n")
        assert request("/api/sync", {"repository_id": "demo"})[0] == 409
        assert (fork / "dirty.txt").read_text() == "keep\n"


def test_shared_lock_and_git_lock_are_preserved(repositories):
    _, fork = repositories
    with running_server(fork) as (_, request):
        with (fork / ".git" / "upstream-synchronizer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for route in ("/api/compare", "/api/sync"):
                assert request(route, {"repository_id": "demo"})[0] == 503
        lock = fork / ".git" / "index.lock"
        lock.write_bytes(b"preserve lock")
        for route in ("/api/compare", "/api/sync"):
            assert request(route, {"repository_id": "demo"})[0] == 409
        assert lock.read_bytes() == b"preserve lock"


@pytest.mark.parametrize(("headers", "data", "status"), [
    ({"X-Sync-Token": ""}, {"repository_id": "demo"}, 403),
    ({"Origin": "https://foreign.example"}, {"repository_id": "demo"}, 403),
    ({"Host": "foreign.example"}, {"repository_id": "demo"}, 403),
    ({}, {"repository_id": "/etc"}, 400),
    ({}, {"repository_id": ["demo"]}, 400),
    ({}, {"repository_id": "demo", "path": "/etc"}, 400),
    ({"Content-Type": "text/plain"}, {"repository_id": "demo"}, 400),
])
def test_invalid_http_requests_have_no_git_effect(repositories, headers, data, status):
    _, fork = repositories
    before = git(fork, "rev-parse", "HEAD")
    with running_server(fork) as (_, request):
        assert request("/api/sync", data, headers)[0] == status
        assert request("/api/sync")[0] == 404
        assert request("/../sync.py")[0] == 404
    assert git(fork, "rev-parse", "HEAD") == before
    assert not (fork / ".git" / "FETCH_HEAD").exists()


def test_comparison_rejects_inherited_git_context(repositories, monkeypatch):
    _, fork = repositories
    monkeypatch.setenv("GIT_DIR", str(fork / ".git"))
    with pytest.raises(SyncError, match="repository-local Git environment"):
        comparison.compare_repository(fork)
    assert not (fork / ".git" / "upstream-synchronizer.lock").exists()
