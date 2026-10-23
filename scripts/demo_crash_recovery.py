"""Demonstrate redelivery after a worker exits between Git update and job completion."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import camunda_orchestration_sdk as sdk

from demo import PROJECT, camunda_demo_lock, cancel_demo_instances, git, prepare, wait_for
from git_upstream_synchronizer.camunda_worker import JOB_TYPE, handle_job, load_repositories

CRASH_EXIT_CODE = 23
DEMO_JOB_TIMEOUT_MS = 15_000


async def crash_worker(root: Path) -> None:
    """Run the production handler, then terminate before the SDK can acknowledge it."""
    repositories = load_repositories(root / "repos.json")

    def crash_after_update(job):
        result = handle_job(job, repositories)
        assert result["status"] == "UPDATED", result
        receipt = {"job_key": str(job.job_key), "retries": job.retries,
                   "deadline": job.deadline, **result}
        (root / "before-crash.json").write_text(json.dumps(receipt, indent=2) + "\n")
        # Deliberately skip normal Python/SDK cleanup and job completion.
        os._exit(CRASH_EXIT_CODE)

    async with sdk.CamundaAsyncClient(logger=logging.getLogger(__name__)) as client:
        client.create_job_worker(
            config=sdk.WorkerConfig(job_type=JOB_TYPE,
                job_timeout_milliseconds=DEMO_JOB_TIMEOUT_MS, max_concurrent_jobs=1,
                fetch_variables=["repository_id"], worker_name="git-upstream-crash-demo"),
            callback=crash_after_update, execution_strategy="thread",
        )
        await client.run_workers()


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def demonstrate(root: Path, lock_fd: int) -> dict:
    fork = root / "fork"
    before = git(fork, "rev-parse", "HEAD")
    upstream = git(root / "seed", "rev-parse", "HEAD")
    with sdk.CamundaClient() as client:
        client.deploy_resources_from_files([PROJECT / "bpmn/synchronize-repository.bpmn"])
        instance = client.create_process_instance(data=sdk.ProcessCreationById(
            process_definition_id="git-upstream-sync", variables=
            sdk.ProcessInstanceCreationInstructionByIdVariables.from_dict({"repository_id": "demo"})))
        key = instance.process_instance_key
        children = []
        finished = False
        try:
            with (root / "crash-worker.log").open("w") as log:
                worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--crash-worker", str(root)], stdout=log, stderr=log, pass_fds=(lock_fd,))
                children.append(worker)
                assert worker.wait(timeout=40) == CRASH_EXIT_CODE, "Fault worker did not exit as expected"
            receipt = json.loads((root / "before-crash.json").read_text())
            assert receipt["previous_sha"] == before
            assert receipt["current_sha"] == upstream
            assert git(fork, "rev-parse", "HEAD") == upstream
            assert (fork / "upstream.txt").is_file()
            count = git(fork, "rev-list", "--count", "HEAD")

            def jobs():
                return client.search_jobs(data=sdk.JobSearchQuery(
                    filter_=sdk.JobSearchQueryFilter(process_instance_key=key))).items

            def variables():
                items = client.search_variables(data=sdk.VariableSearchQuery(
                    filter_=sdk.VariableSearchQueryFilter(process_instance_key=key))).items
                return {item.name: json.loads(item.value) for item in items}

            def timed_out_job():
                items = jobs()
                return items[0] if len(items) == 1 and items[0].state.value == "TIMED_OUT" else None

            timed_out = wait_for(timed_out_job, "unacknowledged job timeout", seconds=45)
            assert str(timed_out.job_key) == receipt["job_key"]
            assert timed_out.retries == receipt["retries"] == 3
            assert timed_out.worker == "git-upstream-crash-demo"
            assert "status" not in variables()
            print(f"Worker exited after UPDATED: process={key}, job={receipt['job_key']}, retries=3", flush=True)
            print("Activation timed out without decrementing retries", flush=True)

            with (root / "recovery-worker.log").open("w") as log:
                replacement = subprocess.Popen([sys.executable, "-m",
                    "git_upstream_synchronizer.camunda_worker", "--repositories", str(root / "repos.json")],
                    stdout=log, stderr=log, pass_fds=(lock_fd,))
                children.append(replacement)

                def completed():
                    items = client.search_process_instances(data=sdk.ProcessInstanceSearchQuery(
                        filter_=sdk.ProcessInstanceSearchQueryFilter(process_instance_key=key))).items
                    return items and items[0].state.value == "COMPLETED"

                wait_for(completed, "job timeout redelivery and process completion", seconds=60)
                stop(replacement)
            recovered = wait_for(lambda: variables().get("status"), "completion variables")
            assert recovered == "UP_TO_DATE", recovered

            def completed_job():
                items = jobs()
                return items[0] if len(items) == 1 and items[0].state.value == "COMPLETED" else None

            final = wait_for(completed_job, "completed job history")
            assert str(final.job_key) == receipt["job_key"]
            assert final.retries == 3
            assert final.worker == "git-upstream-synchronizer"
            assert git(fork, "rev-parse", "HEAD") == upstream
            assert git(fork, "rev-list", "--count", "HEAD") == count
            assert not client.search_incidents(data=sdk.IncidentSearchQuery(
                filter_=sdk.IncidentSearchQueryFilter(process_instance_key=key))).items
            end = wait_for(lambda: client.search_element_instances(data=sdk.ElementInstanceSearchQuery(
                filter_=sdk.ElementInstanceSearchQueryFilter(process_instance_key=key,
                    element_id="EndUpToDate"))).items, "UP_TO_DATE end event")
            assert end
            report = {"process_instance_key": str(key), "job_key": receipt["job_key"],
                      "crashed_worker_exit_code": CRASH_EXIT_CODE, "demo_job_timeout_ms": DEMO_JOB_TIMEOUT_MS,
                      "first_result": receipt, "timeout_state": "TIMED_OUT",
                      "redelivery_status": recovered, "remaining_retries": final.retries,
                      "current_sha": upstream, "commit_count": int(count), "end_event": "EndUpToDate"}
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(f"Same job redelivered: status={recovered}, retries=3, HEAD unchanged", flush=True)
            finished = True
            return report
        finally:
            for child in children:
                stop(child)
            if not finished:
                cancel_demo_instances(client, [key])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="New fixture directory; must not exist")
    parser.add_argument("--crash-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ.setdefault("CAMUNDA_REST_ADDRESS", "http://127.0.0.1:8080/v2")
    os.environ.setdefault("CAMUNDA_AUTH_STRATEGY", "NONE")
    if args.crash_worker:
        asyncio.run(crash_worker(args.crash_worker))
        return
    root = prepare(args.directory)
    print(f"Demo directory: {root}", flush=True)
    with camunda_demo_lock() as lock_fd:
        demonstrate(root, lock_fd)
    print(f"Evidence saved: {root / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
