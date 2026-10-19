"""The CLI uses the same isolated repositories as the core tests."""

import json
from git_upstream_synchronizer.cli import main
from test_sync import repositories, commit_file, git


def test_cli_update_and_repeat(repositories, capsys):
    seed, fork = repositories
    commit_file(seed, "cli.txt", "visible change\n")
    git(seed, "push", "origin", "main")
    assert main([str(fork)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "UPDATED"
    assert main([str(fork)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "UP_TO_DATE"


def test_cli_review(repositories, capsys):
    _, fork = repositories
    commit_file(fork, "local.txt", "local\n")
    assert main([str(fork)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "NEEDS_REVIEW"


def test_cli_failure(tmp_path, capsys):
    assert main([str(tmp_path / "missing")]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "unavailable" in json.loads(output.err)["error"]
