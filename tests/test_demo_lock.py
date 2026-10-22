"""Automated demonstrations must not consume one another's Camunda jobs."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1]
DEMO_SPEC = importlib.util.spec_from_file_location("synchronizer_demo", SOURCE / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(DEMO_SPEC)
DEMO_SPEC.loader.exec_module(demo)


@pytest.fixture
def demo_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in ("demo.py", "demo_crash_recovery.py"):
        shutil.copyfile(SOURCE / "scripts" / name, project / "scripts" / name)
    monkeypatch.setattr(demo, "PROJECT", project)
    return project


def invoke(project, script, directory, *args):
    return subprocess.run(
        [sys.executable, str(project / "scripts" / script), "--directory", str(directory), *args],
        env={**os.environ, "PYTHONPATH": str(SOURCE / "src"),
             "CAMUNDA_REST_ADDRESS": "http://127.0.0.1:1/v2", "CAMUNDA_AUTH_STRATEGY": "NONE"},
        text=True, capture_output=True, timeout=15,
    )


@pytest.mark.parametrize("script", ["demo.py", "demo_crash_recovery.py"])
def test_automated_demo_refuses_overlap(demo_project, tmp_path, script):
    if script == "demo_crash_recovery.py" and importlib.util.find_spec("camunda_orchestration_sdk") is None:
        pytest.skip("Optional Camunda SDK is absent")
    args = ("--camunda",) if script == "demo.py" else ()
    with demo.camunda_demo_lock():
        result = invoke(demo_project, script, tmp_path / "fixtures", *args)
    assert result.returncode == 1, result.stderr
    assert "Another Camunda demonstration is running" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "fixtures" / "report.json").exists()


def test_completed_demo_lock_file_allows_another_run(demo_project):
    with demo.camunda_demo_lock():
        pass
    assert (demo_project / "infra" / "camunda" / "demonstration.lock").exists()
    with demo.camunda_demo_lock():
        pass


def test_surviving_worker_retains_demo_lock(demo_project):
    child = None
    try:
        with demo.camunda_demo_lock() as lock_fd:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(15)"],
                pass_fds=(lock_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        assert child.poll() is None
        with pytest.raises(SystemExit, match="Another Camunda demonstration"):
            with demo.camunda_demo_lock():
                pytest.fail("Guard was released while the worker was alive")
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=5)
    with demo.camunda_demo_lock():
        pass


def test_standalone_demo_remains_available_while_camunda_demo_runs(demo_project, tmp_path):
    with demo.camunda_demo_lock():
        result = invoke(demo_project, "demo.py", tmp_path / "fixtures")
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "fixtures" / "report.json").read_text())
    assert [item["status"] for item in report["results"]] == ["UPDATED", "UP_TO_DATE", "NEEDS_REVIEW"]
