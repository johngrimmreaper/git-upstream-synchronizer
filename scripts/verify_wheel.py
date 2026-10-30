"""Install a supplied wheel in a new venv and smoke-test installed entry points.

Optional --camunda also executes an installed worker and web adapter against the
running local runtime. Run it sequentially with the other live demonstrations.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile

from demo import PROJECT, camunda_demo_lock, cancel_demo_instances, git, prepare, wait_for
from test_browser_integration import stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--camunda", action="store_true")
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    root = prepare(None)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.setdefault("CAMUNDA_REST_ADDRESS", "http://127.0.0.1:8080/v2")
    environment.setdefault("CAMUNDA_AUTH_STRATEGY", "NONE")
    venv = root / "installed"
    subprocess.run(["python3", "-m", "venv", str(venv)], check=True)
    python = str(venv / "bin/python")
    subprocess.run([python, "-m", "pip", "install", str(wheel) + ("[camunda]" if args.camunda else "")],
                   check=True, cwd=root, env=environment, timeout=180)
    code = "import git_upstream_synchronizer as m; print(m.__file__); assert 'site-packages' in m.__file__"
    if not args.camunda:
        code += "; import importlib.util; assert importlib.util.find_spec('camunda_orchestration_sdk') is None"
    subprocess.run([python, "-c", code], cwd=root, env=environment, check=True)
    result = subprocess.run([str(venv / "bin/git-upstream-sync"), str(root / "fork")],
                            cwd=root, env=environment, check=True, text=True, capture_output=True)
    assert json.loads(result.stdout)["status"] == "UPDATED"
    if not args.camunda:
        result = subprocess.run([str(venv / "bin/git-upstream-web"), "--repositories", str(root / "repos.json"),
                                 "--execution", "camunda"], cwd=root, env=environment, text=True, capture_output=True)
        assert result.returncode == 2 and "requires git-upstream-synchronizer[camunda]" in result.stderr
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    mode = "camunda" if args.camunda else "direct"

    def request(path, data=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            body = None if data is None else json.dumps(data)
            connection.request("GET" if data is None else "POST", path, body=body,
                headers={} if data is None else {"Content-Type": "application/json", "X-Sync-Token": token})
            response = connection.getresponse()
            output = response.read().decode()
            assert response.status in (200, 202), output
            return output if path in ("/", "/app.js", "/app.css") else json.loads(output)
        finally:
            connection.close()

    def ready():
        try: return request("/")
        except (OSError, http.client.HTTPException): return None

    with (root / "installed-web.log").open("w") as log:
        web = subprocess.Popen([str(venv / "bin/git-upstream-web"), "--repositories", str(root / "repos.json"),
            "--port", str(port), "--execution", mode], cwd=root, env=environment, stdout=log, stderr=log)
        try:
            html = wait_for(ready, "installed web readiness", seconds=20)
            token = re.search('name="sync-token" content="([^"]+)"', html).group(1)
            assert "function" in request("/app.js") and ":root" in request("/app.css")
            assert request("/api/repositories")["execution"] == mode
            assert request("/api/compare", {"repository_id": "failure"}, token)["status"] == "BEHIND"
            if args.camunda:
                with camunda_demo_lock() as lock_fd, (root / "installed-worker.log").open("w") as worker_log:
                    # Deploy using the installed SDK outside the checkout.
                    subprocess.run([python, "-c", "from camunda_orchestration_sdk import CamundaClient; "
                        "\nwith CamundaClient() as c:\n c.get_topology(); c.deploy_resources_from_files([" +
                        repr(str(PROJECT / "bpmn/synchronize-repository.bpmn")) + "])"],
                        cwd=root, env=environment, check=True)
                    worker = subprocess.Popen([str(venv / "bin/git-upstream-worker"), "--repositories", str(root / "repos.json")],
                        cwd=root, env=environment, stdout=worker_log, stderr=worker_log, pass_fds=(lock_fd,))
                    operation = None
                    verified = False
                    try:
                        operation = request("/api/sync", {"repository_id": "failure"}, token)
                        def completed():
                            result = request("/api/operation", {"repository_id": "failure", "operation_id": operation["operation_id"]}, token)
                            return result if result["state"] == "COMPLETED" else None
                        result = wait_for(completed, "installed Camunda workflow")
                        assert result["result"]["status"] == "UPDATED" and result["end_event"] == "EndUpdated"
                        (root / "installed-result.json").write_text(json.dumps(result, indent=2) + "\n")
                        verified = True
                    finally:
                        stop(worker)
                        if not verified and operation and operation.get("process_instance_key"):
                            try:
                                import camunda_orchestration_sdk as sdk
                                with sdk.CamundaClient(configuration={key: environment[key] for key in
                                     ("CAMUNDA_REST_ADDRESS", "CAMUNDA_AUTH_STRATEGY")}) as cleanup:
                                    cancel_demo_instances(cleanup, [operation["process_instance_key"]])
                            except Exception as error:
                                print(f"Installed-wheel cleanup failed: {error}")
            else:
                assert request("/api/sync", {"repository_id": "failure"}, token)["status"] == "UPDATED"
            assert git(root / "failure-fork", "rev-parse", "HEAD") == git(root / "seed", "rev-parse", "HEAD")
        finally:
            stop(web)
    print(f"PASS: installed wheel ({mode}), evidence={root}", flush=True)


if __name__ == "__main__":
    main()
