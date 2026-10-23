"""Reproduce Git outcomes using newly created local repositories only."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def camunda_demo_lock():
    """Serialize automated Camunda demos; workers inherit the held descriptor."""
    lock_path = PROJECT / "infra" / "camunda" / "demonstration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "Another Camunda demonstration is running from this checkout; "
                "wait for it to finish. Do not delete demonstration.lock."
            ) from None
        yield lock.fileno()


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                          capture_output=True, text=True, timeout=15).stdout.strip()


def commit(repo: Path, filename: str) -> None:
    (repo / filename).write_text(f"Demo: {filename}\n", encoding="utf-8")
    git(repo, "add", filename)
    # Disposable fixture history, never commits to the project or a real repository.
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", f"Add {filename}")


def prepare(directory: Path | None) -> Path:
    if directory is None:
        root = Path(tempfile.mkdtemp(prefix="git-sync-demo-"))
    else:
        root = directory.resolve()
        root.mkdir(parents=True, exist_ok=False)  # Never reuse or erase user data.
    git(root, "init", "--bare", "upstream.git")
    git(root, "clone", "upstream.git", "seed")
    seed = root / "seed"
    git(seed, "config", "user.name", "Demo Author")
    git(seed, "config", "user.email", "demo@example.invalid")
    git(seed, "switch", "-c", "main")
    commit(seed, "initial.txt")
    git(seed, "push", "origin", "main")
    for name in ("fork", "failure-fork"):
        git(root, "clone", "-b", "main", "upstream.git", name)
        git(root / name, "remote", "rename", "origin", "upstream")
        git(root / name, "config", "user.name", "Demo Author")
        git(root / name, "config", "user.email", "demo@example.invalid")
    commit(seed, "upstream.txt")
    git(seed, "push", "origin", "main")
    config = {"demo": {"path": str(root / "fork")},
              "failure": {"path": str(root / "failure-fork")}}
    (root / "repos.json").write_text(json.dumps(config, indent=2) + "\n")
    return root


def exercise(root: Path, invoke) -> list[dict]:
    results = []
    for expected in ("UPDATED", "UP_TO_DATE", "NEEDS_REVIEW"):
        if expected == "NEEDS_REVIEW":
            commit(root / "fork", "local.txt")
            commit(root / "seed", "independent.txt")
            git(root / "seed", "push", "origin", "main")
        before = git(root / "fork", "rev-parse", "HEAD")
        result = invoke("demo")
        assert result["status"] == expected, result
        after = git(root / "fork", "rev-parse", "HEAD")
        assert result["current_sha"] == after
        if expected == "UPDATED":
            assert before != after and (root / "fork" / "upstream.txt").is_file()
        else:
            assert before == after
        results.append(result)
        print(json.dumps(result), flush=True)
    assert (root / "fork" / "local.txt").is_file()
    assert not (root / "fork" / "independent.txt").exists()
    return results


def wait_for(check, description: str, seconds: int = 90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.3)
    raise TimeoutError(f"Timed out waiting for {description}")


def cancel_demo_instances(client, keys) -> None:
    """Best-effort cancellation of instances created by an unsuccessful demo."""
    from camunda_orchestration_sdk import CancelProcessInstanceData
    from camunda_orchestration_sdk.errors import NotFoundError

    for key in keys:
        try:
            client.cancel_process_instance(key, data=CancelProcessInstanceData())
        except NotFoundError:
            pass  # The instance has already finished or been canceled.
        except Exception as error:
            print(f"Could not cancel demo process {key}; inspect it in Operate "
                  f"({type(error).__name__})", file=sys.stderr)


def run_camunda(root: Path, lock_fd: int) -> dict:
    # The optional SDK is imported only for this demonstration backend.
    import camunda_orchestration_sdk as sdk
    environment = {**os.environ, "CAMUNDA_REST_ADDRESS": os.environ.get(
        "CAMUNDA_REST_ADDRESS", "http://127.0.0.1:8080/v2"),
        "CAMUNDA_AUTH_STRATEGY": os.environ.get("CAMUNDA_AUTH_STRATEGY", "NONE")}
    keys = []
    with sdk.CamundaClient(configuration={key: environment[key] for key in
             ("CAMUNDA_REST_ADDRESS", "CAMUNDA_AUTH_STRATEGY")}) as client:
        client.deploy_resources_from_files([PROJECT / "bpmn/synchronize-repository.bpmn"])
        with (root / "worker.log").open("w") as log:
            worker = subprocess.Popen([sys.executable, "-m", "git_upstream_synchronizer.camunda_worker",
                "--repositories", str(root / "repos.json")], env=environment, stdout=log, stderr=log,
                pass_fds=(lock_fd,))
            verified = False
            try:
                def start(repository_id):
                    result = client.create_process_instance(data=sdk.ProcessCreationById(
                        process_definition_id="git-upstream-sync", variables=
                        sdk.ProcessInstanceCreationInstructionByIdVariables.from_dict(
                            {"repository_id": repository_id})))
                    keys.append(str(result.process_instance_key))
                    return result.process_instance_key

                def variables(key):
                    result = client.search_variables(data=sdk.VariableSearchQuery(
                        filter_=sdk.VariableSearchQueryFilter(process_instance_key=key)))
                    return {item.name: json.loads(item.value) for item in result.items}

                def result_variables(key):
                    values = variables(key)
                    return values if "status" in values else None

                def completed(key):
                    result = client.search_process_instances(data=sdk.ProcessInstanceSearchQuery(
                        filter_=sdk.ProcessInstanceSearchQueryFilter(process_instance_key=key)))
                    return result.items and result.items[0].state.value == "COMPLETED"

                def completed_result(key):
                    wait_for(lambda: completed(key), f"process {key} completion")
                    result = wait_for(lambda: result_variables(key), "result variables")
                    expected_end = {"UPDATED": "EndUpdated", "UP_TO_DATE": "EndUpToDate",
                                    "NEEDS_REVIEW": "EndNeedsReview"}[result["status"]]
                    def end_reached():
                        return client.search_element_instances(data=sdk.ElementInstanceSearchQuery(
                            filter_=sdk.ElementInstanceSearchQueryFilter(process_instance_key=key,
                                element_id=expected_end))).items
                    wait_for(end_reached, expected_end)
                    return {**result, "process_instance_key": str(key), "end_event": expected_end}

                def invoke(repository_id):
                    return completed_result(start(repository_id))

                results = exercise(root, invoke)
                fork = root / "failure-fork"
                before = git(fork, "rev-parse", "HEAD")
                unavailable = str(root / "temporarily-unavailable.git")
                git(fork, "remote", "set-url", "upstream", unavailable)
                key = start("failure")
                def failed_job():
                    jobs = client.search_jobs(data=sdk.JobSearchQuery(
                        filter_=sdk.JobSearchQueryFilter(process_instance_key=key))).items
                    return next((j for j in jobs if j.retries == 2), None)
                wait_for(failed_job, "first failed attempt with two retries left")
                assert git(fork, "rev-parse", "HEAD") == before
                print(f"Temporary fetch failure observed: process={key}, retries=2", flush=True)
                git(fork, "remote", "set-url", "upstream", str(root / "upstream.git"))
                wait_for(lambda: completed(key), "automatic retry recovery")
                recovered = wait_for(lambda: result_variables(key), "recovered result")
                assert recovered["status"] == "UPDATED"
                assert git(fork, "rev-parse", "HEAD") == git(root / "seed", "rev-parse", "HEAD")
                print(f"Automatic retry recovered: process={key}, status={recovered['status']}", flush=True)
                git(fork, "remote", "set-url", "upstream", unavailable)
                incident_key = start("failure")
                def incident(process_key, state="ACTIVE"):
                    items = client.search_incidents(data=sdk.IncidentSearchQuery(
                        filter_=sdk.IncidentSearchQueryFilter(process_instance_key=process_key))).items
                    return next((i for i in items if i.state.value == state), None)
                active = wait_for(lambda: incident(incident_key), "incident after exhausted retries")
                jobs = client.search_jobs(data=sdk.JobSearchQuery(
                    filter_=sdk.JobSearchQueryFilter(process_instance_key=incident_key))).items
                assert jobs and jobs[0].retries == 0
                assert git(fork, "rev-parse", "HEAD") == recovered["current_sha"]
                assert "status" not in variables(incident_key)
                print(f"Active incident: process={incident_key}, retries=0, incident={active.incident_key}", flush=True)
                git(fork, "remote", "set-url", "upstream", str(root / "upstream.git"))
                lock = fork / ".git" / "index.lock"
                lock_content = b"isolated demo Git lock\n"
                with lock.open("xb") as fixture:
                    fixture.write(lock_content)
                try:
                    blocked_key = start("failure")
                    blocked = wait_for(lambda: incident(blocked_key), "Git lock incident")
                    blocked_jobs = client.search_jobs(data=sdk.JobSearchQuery(
                        filter_=sdk.JobSearchQueryFilter(process_instance_key=blocked_key))).items
                    assert len(blocked_jobs) == 1 and blocked_jobs[0].retries == 0
                    assert "Git lock file: index.lock" in blocked_jobs[0].error_message
                    assert lock.read_bytes() == lock_content
                    assert git(fork, "rev-parse", "HEAD") == recovered["current_sha"]
                    assert "status" not in variables(blocked_key)
                    print(f"Git lock refused: process={blocked_key}, retries=0, lock preserved", flush=True)
                finally:
                    # Only the demo removes the synthetic lock it created.
                    if lock.exists() and lock.read_bytes() == lock_content:
                        lock.unlink()
                # Operator recovery: fix the cause, restore retries, resolve the incident.
                locked_job_key = blocked_jobs[0].job_key
                client.update_job(locked_job_key, data=sdk.JobUpdateRequest(
                    changeset=sdk.JobChangeset(retries=3)))
                client.resolve_incident(blocked.incident_key)
                repaired = completed_result(blocked_key)
                assert repaired["status"] == "UP_TO_DATE"
                assert repaired["previous_sha"] == repaired["current_sha"] == recovered["current_sha"]
                assert git(fork, "rev-parse", "HEAD") == recovered["current_sha"]

                def completed_recovery_job():
                    items = client.search_jobs(data=sdk.JobSearchQuery(
                        filter_=sdk.JobSearchQueryFilter(process_instance_key=blocked_key))).items
                    return items[0] if len(items) == 1 and items[0].state.value == "COMPLETED" else None

                resumed = wait_for(completed_recovery_job, "same job completion after incident resolution")
                assert str(resumed.job_key) == str(locked_job_key)
                assert str(resumed.process_instance_key) == str(blocked_key)
                assert resumed.retries == 3
                resolved = wait_for(lambda: incident(blocked_key, "RESOLVED"), "Git lock incident resolution")
                assert str(resolved.incident_key) == str(blocked.incident_key)
                assert incident(blocked_key) is None
                repaired.update(job_key=str(resumed.job_key), remaining_retries=resumed.retries)
                print(f"Resolved Git lock incident: process={blocked_key}, job={locked_job_key}, "
                      "status=UP_TO_DATE, HEAD unchanged", flush=True)
                report = {"results": results, "retry_recovered_process": str(key),
                        "incident_process": str(incident_key), "incident_key": str(active.incident_key),
                        "git_lock_incident_process": str(blocked_key),
                        "git_lock_incident_key": str(blocked.incident_key),
                        "git_lock_job_key": str(locked_job_key),
                        "git_lock_incident_state": resolved.state.value,
                        "git_lock_recovery": repaired, "process_instance_keys": keys}
                verified = True
                return report
            finally:
                try:
                    worker.terminate()
                    try:
                        worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(timeout=5)
                finally:
                    if not verified:
                        cancel_demo_instances(client, keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camunda", action="store_true", help="Exercise a running local Camunda instance")
    parser.add_argument("--prepare", action="store_true", help="Only prepare isolated repos for a manual demonstration")
    parser.add_argument("--directory", type=Path, help="New directory; must not already exist")
    args = parser.parse_args()
    root = prepare(args.directory)
    print(f"Demo directory: {root}", flush=True)
    if args.prepare:
        print(f"Worker configuration: {root / 'repos.json'}")
        return
    if args.camunda:
        with camunda_demo_lock() as lock_fd:
            report = run_camunda(root, lock_fd)
    else:
        def invoke(repository_id):
            result = subprocess.run([sys.executable, "-m", "git_upstream_synchronizer.cli",
                str(root / "fork")], text=True, capture_output=True, timeout=60)
            assert result.returncode in (0, 2), result.stderr
            return json.loads(result.stdout)
        report = {"results": exercise(root, invoke)}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Evidence saved: {root / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
