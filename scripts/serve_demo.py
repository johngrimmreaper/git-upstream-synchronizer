"""Supervise one Camunda-backed web/worker demonstration on loopback.

Creates new disposable fixtures unless --resume reuses a previous demo directory.
Ctrl+C or SIGTERM stops only the two children this supervisor launched.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

from demo import PROJECT, camunda_demo_lock, commit, git, prepare, wait_for
from test_browser_integration import stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.directory.resolve()
    os.environ.setdefault("CAMUNDA_REST_ADDRESS", "http://127.0.0.1:8080/v2")
    os.environ.setdefault("CAMUNDA_AUTH_STRATEGY", "NONE")
    if args.resume:
        config = root / "web-repos.json"
        if not config.is_file(): parser.error("Existing demonstration configuration is missing")
    else:
        root = prepare(root)
        commit(root / "failure-fork", "preserved-local.txt")
        git(root, "clone", "--no-hardlinks", str(root / "fork"), "smoke")
        git(root / "smoke", "remote", "rename", "origin", "upstream")
        git(root / "smoke", "remote", "set-url", "upstream", str(root / "upstream.git"))
        config = root / "web-repos.json"
        config.write_text(json.dumps({"ready": {"path": str(root / "fork")},
            "review": {"path": str(root / "failure-fork")}, "smoke": {"path": str(root / "smoke")}}, indent=2) + "\n")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    children = []
    import camunda_orchestration_sdk as sdk
    with camunda_demo_lock() as lock_fd, sdk.CamundaClient() as client:
        topology = client.get_topology()
        print(f"Camunda gateway ready: {topology.gateway_version}", flush=True)
        pending = client.search_jobs(data=sdk.JobSearchQuery(
            filter_=sdk.JobSearchQueryFilter(type_="synchronize-repository",
                retries=sdk.AdvancedIntegerFilter(gt=0), state=sdk.AdvancedJobStateFilter(in_=[
                    sdk.JobStateEnum.CREATED, sdk.JobStateEnum.FAILED,
                    sdk.JobStateEnum.TIMED_OUT, sdk.JobStateEnum.RETRIES_UPDATED])),
            page=sdk.LimitBasedPagination(limit=1))).items
        if pending:
            raise RuntimeError("Eligible jobs already exist; inspect their repository mapping before starting a worker")
        client.deploy_resources_from_files([PROJECT / "bpmn/synchronize-repository.bpmn"])
        try:
            for name, module, extra in [
                ("worker", "camunda_worker", []),
                ("web", "web", ["--execution", "camunda", "--port", "8089"]),
            ]:
                with (root / (name + ".log")).open("a") as log:
                    child = subprocess.Popen([sys.executable, "-m", "git_upstream_synchronizer." + module,
                        "--repositories", str(config), *extra], stdout=log, stderr=log, pass_fds=(lock_fd,))
                children.append(child)
            metadata = {"supervisor_pid": os.getpid(), "worker_pid": children[0].pid,
                        "web_pid": children[1].pid, "configuration": str(config),
                        "web_url": "http://127.0.0.1:8089", "operate_url": "http://127.0.0.1:8080/operate"}
            (root / "stack.json").write_text(json.dumps(metadata, indent=2) + "\n")
            def ready():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("A demo child exited; inspect its log")
                try:
                    with urlopen("http://127.0.0.1:8089/api/repositories", timeout=2) as response:
                        result = json.load(response)
                    return result["execution"] == "camunda" and {r["id"] for r in result["repositories"]} == {"ready", "review", "smoke"}
                except OSError:
                    return False
            wait_for(ready, "demonstration web readiness", seconds=20)
            print(json.dumps(metadata), flush=True)
            while all(child.poll() is None for child in children):
                time.sleep(1)
            raise RuntimeError("A demo child exited; inspect its log")
        except KeyboardInterrupt:
            print("Stopping demonstration children", flush=True)
        finally:
            for child in reversed(children): stop(child)


if __name__ == "__main__":
    main()
