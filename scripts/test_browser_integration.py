"""Explicit live browser tests; run with --camunda for the full local workflow.

Requires the dev-only Playwright package and its Chromium download. Uses disposable
Git repositories, test-owned HTTP servers, and the existing demonstration guard.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from demo import PROJECT, camunda_demo_lock, cancel_demo_instances, commit, git, prepare, wait_for
from git_upstream_synchronizer.web import WebServer


@contextmanager
def served(repositories, executor=None):
    server = WebServer(repositories, port=0, executor=executor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


def stop(child):
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    except Exception as error:
        print(f"Could not stop owned child {child.pid}: {error}", file=sys.stderr)


def fixtures(root):
    names = ["behind", "retry", "persistent", "dirty", "lock", "stale", "duplicate", "unavailable", "crash", "ahead", "diverged"]
    mapping = {}
    for name in names:
        git(root, "clone", "--no-hardlinks", str(root / "fork"), name)
        path = root / name
        git(path, "remote", "rename", "origin", "upstream")
        git(path, "remote", "set-url", "upstream", str(root / "upstream.git"))
        git(path, "config", "user.name", "Demo Author")
        git(path, "config", "user.email", "demo@example.invalid")
        mapping[name] = {"path": str(path)}
    seed = root / "seed"
    filename = '<script>alert("filename")</script>.txt'.replace('/', '_')
    (seed / filename).write_text('<img src=x onerror="window.injected=true">\n')
    git(seed, "add", ".")
    git(seed, "-c", "commit.gpgsign=false", "commit", "-m", '<script>window.injected=true</script>')
    git(seed, "push", "origin", "main")
    git(root / "ahead", "fetch", "upstream", "main")
    git(root / "ahead", "merge", "--ff-only", "FETCH_HEAD")
    commit(root / "ahead", "local.txt")
    commit(root / "diverged", "local.txt")
    return mapping, filename


def http(page, action, **data):
    return page.evaluate("""async ({action, data}) => {
      const response = await fetch('/api/' + action, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Sync-Token': document.querySelector('meta[name="sync-token"]').content},
        body: JSON.stringify(data)});
      return {code: response.status, body: await response.json()};
    }""", {"action": action, "data": data})


def select(page, name, title="Upstream changes are ready to sync"):
    page.locator("#repository").select_option(name)
    page.locator("#state-title").filter(has_text=title).wait_for(timeout=20000)
    page.locator("#repository:not([disabled])").wait_for(timeout=20000)


def open_page(browser, url, errors):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("pageerror", lambda error: errors.append({"type": "pageerror", "text": str(error)}))
    page.on("console", lambda msg: errors.append({"type": "console", "text": msg.text}) if msg.type == "error" else None)
    page.goto(url)
    page.locator("#state-title").filter(has_text="Upstream changes are ready to sync").wait_for(timeout=20000)
    return page


def direct(browser, root, mapping, filename, report, errors):
    with served({"behind": mapping["behind"], "ahead": mapping["ahead"], "diverged": mapping["diverged"]}) as (_, url):
        page = open_page(browser, url, errors)
        assert "2 commits behind" in page.locator("#counts").inner_text()
        assert '<script>window.injected=true</script>' in page.locator("#upstream-commits").inner_text()
        page.locator("#files-tab").click()
        detail = page.locator("details").filter(has_text=filename)
        detail.locator("summary").click()
        detail.locator(".patch").wait_for()
        assert '<img src=x onerror="window.injected=true">' in detail.inner_text()
        assert page.evaluate("window.injected") is None
        assert page.locator("#files script, #files img").count() == 0
        page.screenshot(path=str(root / "direct-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(root / "direct-mobile.png"), full_page=True)
        # A real browser request failure must leave Refresh usable.
        page.route("**/api/compare", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic comparison failure"}'))
        page.locator("#refresh").click()
        page.locator("#message.error").wait_for()
        assert page.locator("#refresh").is_enabled()
        page.unroute("**/api/compare")
        page.locator("#refresh").click()
        page.locator("#sync:not([disabled])").wait_for()
        before = git(root / "behind", "rev-parse", "HEAD")
        page.locator("#sync").click()
        page.locator("#message.success").filter(has_text="UPDATED").wait_for(timeout=20000)
        after = git(root / "behind", "rev-parse", "HEAD")
        assert before != after and (root / "behind" / filename).read_text().startswith('<img')
        assert page.locator("#sync").is_disabled()
        select(page, "ahead", "Your local fork is ahead")
        assert page.locator("#sync").is_disabled()
        select(page, "diverged", "Branches have diverged")
        assert page.locator("#sync").is_disabled()
        report.append({"scenario": "direct-browser", "before": before, "after": after,
                       "literal_rendering": True, "narrow_layout": True, "error_recovery": True})
        page.close()


def camunda(browser, root, mapping, report, errors, lock_fd):
    import camunda_orchestration_sdk as sdk
    from git_upstream_synchronizer.camunda_web import CamundaExecutor
    # A direct test already updated behind; a new Git fixture restores a behind case.
    repository_id = lambda name: root.name + "-" + name
    mapping = {repository_id(name): config for name, config in mapping.items() if name != "behind"}
    (root / "web-repos.json").write_text(json.dumps(mapping, indent=2) + "\n")
    executor = CamundaExecutor()
    keys = []
    finished = False
    with sdk.CamundaClient() as client, (root / "web-worker.log").open("w") as log:
        pending = client.search_jobs(data=sdk.JobSearchQuery(
            filter_=sdk.JobSearchQueryFilter(type_="synchronize-repository",
                retries=sdk.AdvancedIntegerFilter(gt=0), state=sdk.AdvancedJobStateFilter(in_=[
                    sdk.JobStateEnum.CREATED, sdk.JobStateEnum.FAILED,
                    sdk.JobStateEnum.TIMED_OUT, sdk.JobStateEnum.RETRIES_UPDATED])),
            page=sdk.LimitBasedPagination(limit=1))).items
        if pending:
            raise RuntimeError("Eligible jobs already exist; inspect and coordinate their repository mappings before testing")
        client.deploy_resources_from_files([PROJECT / "bpmn/synchronize-repository.bpmn"])
        injection = root / "crash-injection"
        injection.mkdir()
        (injection / "repos.json").write_text(json.dumps(mapping))
        worker = subprocess.Popen([sys.executable, str(PROJECT / "scripts/demo_crash_recovery.py"),
            "--crash-worker", str(injection)], stdout=log, stderr=log, pass_fds=(lock_fd,))
        children = [worker]
        try:
            with served(mapping, executor) as (server, url):
                page = open_page(browser, url, errors)

                def operation(name):
                    return next(o for o in reversed(list(executor.operations.values())) if o["repository_id"] == repository_id(name))

                def browser_submit(name, mutation=None):
                    select(page, repository_id(name))
                    before = git(root / name, "rev-parse", "HEAD")
                    if mutation: mutation()
                    page.locator("#sync").click()
                    wait_for(lambda: any(o["repository_id"] == repository_id(name) and o.get("process_instance_key") for o in executor.operations.values()), "acknowledged web submission")
                    op = operation(name)
                    key = op["process_instance_key"]
                    assert isinstance(key, str) and key.isdigit(), op
                    keys.append(key)
                    page.locator("#process-key").filter(has_text=key).wait_for(timeout=20000)
                    return op, before

                def status(op):
                    return executor.status(op["operation_id"], op["repository_id"])

                def completed(op, before, expected="UPDATED"):
                    value = wait_for(lambda: (v if (v := status(op))["state"] == "COMPLETED" else None), "web workflow completion")
                    # A paused incident/unavailability view resumes monitoring explicitly.
                    if page.locator("#resume-monitoring").is_visible(): page.locator("#resume-monitoring").click()
                    page.locator("#operation-state").filter(has_text="COMPLETED").wait_for(timeout=20000)
                    page.locator("#repository:not([disabled])").wait_for(timeout=20000)
                    assert value["result"]["status"] == expected
                    assert value["result"]["current_sha"] == git(Path(mapping[op["repository_id"]]["path"]), "rev-parse", "HEAD")
                    assert page.locator("#process-key").inner_text() == op["process_instance_key"]
                    assert page.locator("#operate").get_attribute("href").endswith('/processes/' + op["process_instance_key"])
                    row = {"scenario": op["repository_id"], "before": before,
                           "after": git(Path(mapping[op["repository_id"]]["path"]), "rev-parse", "HEAD"), **value,
                           "browser_state": page.locator("#operation-state").inner_text()}
                    report.append(row)
                    return value

                # Crash is first: no stopped normal worker can retain an activation request.
                fault = worker
                op, before = browser_submit("crash")
                assert fault.wait(timeout=40) == 23
                receipt = json.loads((injection / "before-crash.json").read_text())
                assert receipt["status"] == "UPDATED" and receipt["previous_sha"] == before
                after_effect = git(root / "crash", "rev-parse", "HEAD")
                count = git(root / "crash", "rev-list", "--count", "HEAD")
                def timed_out():
                    jobs = client.search_jobs(data=sdk.JobSearchQuery(filter_=sdk.JobSearchQueryFilter(
                        process_instance_key=op["process_instance_key"]))).items
                    return next((job for job in jobs if job.state.value == "TIMED_OUT"), None)
                timed = wait_for(timed_out, "browser-started job timeout", seconds=45)
                assert timed.retries == 3 and str(timed.job_key) == receipt["job_key"]
                page.locator("#operation-state").filter(has_text="RETRYING").wait_for(timeout=20000)
                report.append({"scenario": "browser-crash-timeout", "process_instance_key": op["process_instance_key"],
                    "job_key": str(timed.job_key), "remaining_retries": timed.retries, "receipt": receipt,
                    "before": before, "after_effect": after_effect, "commit_count": count,
                    "browser_state": page.locator("#operation-state").inner_text()})
                with (root / "browser-recovery-worker.log").open("w") as recovery_log:
                    replacement = subprocess.Popen([sys.executable, "-m", "git_upstream_synchronizer.camunda_worker",
                        "--repositories", str(root / "web-repos.json")], stdout=recovery_log, stderr=recovery_log,
                        pass_fds=(lock_fd,))
                children.append(replacement)
                value = completed(op, before, "UP_TO_DATE")
                assert value["job_key"] == receipt["job_key"] and value["remaining_retries"] == 3
                assert git(root / "crash", "rev-parse", "HEAD") == after_effect
                assert git(root / "crash", "rev-list", "--count", "HEAD") == count
                page.screenshot(path=str(root / "browser-crash-recovered.png"), full_page=True)

                # HTTP/browser request duplication is coalesced before Git execution.
                op, before = browser_submit("duplicate")
                duplicate = http(page, "sync", repository_id=repository_id("duplicate"))
                assert duplicate["code"] == 202 and duplicate["body"]["operation_id"] == op["operation_id"]
                page.reload()
                page.locator("#process-key").filter(has_text=op["process_instance_key"]).wait_for(timeout=20000)
                completed(op, before)
                assert before != git(root / "duplicate", "rev-parse", "HEAD")
                assert (root / "duplicate" / "upstream.txt").exists()
                page.screenshot(path=str(root / "camunda-completed.png"), full_page=True)

                # Idempotency is deliberately exercised through HTTP with the button disabled.
                response = http(page, "sync", repository_id=repository_id("duplicate"))
                repeat = response["body"]
                keys.append(repeat["process_instance_key"])
                value = wait_for(lambda: (v if (v := status(repeat))["state"] == "COMPLETED" else None), "idempotent completion")
                assert value["result"]["status"] == "UP_TO_DATE" and value["end_event"] == "EndUpToDate"
                report.append({"scenario": "repeated-http", **value})

                # Stale clean preview becomes divergent before execution.
                op, before = browser_submit("stale", lambda: commit(root / "stale", "preserve-local.txt"))
                after_local = git(root / "stale", "rev-parse", "HEAD")
                completed(op, before, "NEEDS_REVIEW")
                assert git(root / "stale", "rev-parse", "HEAD") == after_local
                assert not (root / "stale" / "upstream.txt").exists()

                for name, title in [("ahead", "Your local fork is ahead"), ("diverged", "Branches have diverged")]:
                    select(page, repository_id(name), title)
                    assert page.locator("#sync").is_disabled()
                    before = git(root / name, "rev-parse", "HEAD")
                    response = http(page, "sync", repository_id=repository_id(name))["body"]
                    keys.append(response["process_instance_key"])
                    value = wait_for(lambda: (v if (v := status(response))["state"] == "COMPLETED" else None), "review completion")
                    assert value["result"]["status"] == "NEEDS_REVIEW"
                    assert git(root / name, "rev-parse", "HEAD") == before
                    report.append({"scenario": name, "before": before, "after": before, **value})

                def recover(op):
                    jobs = client.search_jobs(data=sdk.JobSearchQuery(filter_=sdk.JobSearchQueryFilter(process_instance_key=op["process_instance_key"]))).items
                    incidents = client.search_incidents(data=sdk.IncidentSearchQuery(filter_=sdk.IncidentSearchQueryFilter(process_instance_key=op["process_instance_key"]))).items
                    job = next(j for j in jobs if j.retries == 0)
                    incident = next(i for i in incidents if i.state.value == "ACTIVE")
                    original = str(job.job_key)
                    op["recovery_job_key"] = original
                    client.update_job(job.job_key, data=sdk.JobUpdateRequest(changeset=sdk.JobChangeset(retries=3)))
                    client.resolve_incident(incident.incident_key)
                    return original

                # Dirty and Git lock states are injected only after a valid preview.
                for name in ["dirty", "lock"]:
                    marker = root / name / ("dirty.txt" if name == "dirty" else ".git/index.lock")
                    content = b"preserve synthetic fixture bytes\n"
                    op, before = browser_submit(name, lambda: marker.write_bytes(content))
                    page.locator("#operation-state").filter(has_text="INCIDENT").wait_for(timeout=20000)
                    incident = status(op)
                    assert marker.read_bytes() == content and git(root / name, "rev-parse", "HEAD") == before
                    report.append({"scenario": name + "-incident", "before": before, "after": before, **incident,
                                   "preserved_bytes": content.decode()})
                    page.screenshot(path=str(root / (name + "-incident.png")), full_page=True)
                    # The test removes only its own known marker as an explicit operator action.
                    assert marker.read_bytes() == content
                    marker.unlink()
                    original = recover(op)
                    value = completed(op, before)
                    assert value["job_key"] == original

                # Failed fetch followed by same-process automatic retry.
                def break_remote(name):
                    git(root / name, "remote", "set-url", "upstream", str(root / "missing.git"))
                op, before = browser_submit("retry", lambda: break_remote("retry"))
                page.locator("#operation-state").filter(has_text="RETRYING").wait_for(timeout=20000)
                failed = status(op)
                assert failed["remaining_retries"] in (1, 2)
                report.append({"scenario": "retry-observed", **failed})
                page.screenshot(path=str(root / "camunda-retrying.png"), full_page=True)
                git(root / "retry", "remote", "set-url", "upstream", str(root / "upstream.git"))
                completed(op, before)

                op, before = browser_submit("persistent", lambda: break_remote("persistent"))
                page.locator("#operation-state").filter(has_text="INCIDENT").wait_for(timeout=30000)
                incident = status(op)
                assert incident["remaining_retries"] == 0 and "result" not in incident
                assert git(root / "persistent", "rev-parse", "HEAD") == before
                report.append({"scenario": "persistent-incident", "before": before, "after": before, **incident})
                page.screenshot(path=str(root / "camunda-incident.png"), full_page=True)
                git(root / "persistent", "remote", "set-url", "upstream", str(root / "upstream.git"))
                original = recover(op)
                value = completed(op, before)
                assert value["job_key"] == original

                # A temporary browser-to-web query failure must retain the process and resume.
                select(page, repository_id("unavailable"))
                page.route("**/api/operation", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic status outage"}'))
                page.locator("#sync").click()
                page.locator("#operation-state").filter(has_text="Monitoring unavailable").wait_for(timeout=20000)
                op = operation("unavailable")
                keys.append(op["process_instance_key"])
                assert page.locator("#sync").is_disabled()
                page.unroute("**/api/operation")
                import httpx
                original_factory = executor.client_factory
                executor.client_factory = lambda: sdk.CamundaClient(configuration={
                    "CAMUNDA_REST_ADDRESS": "http://127.0.0.1:1/v2", "CAMUNDA_AUTH_STRATEGY": "NONE"},
                    timeout=httpx.Timeout(1))
                page.locator("#resume-monitoring").click()
                page.locator("#operation-state").filter(has_text="UNAVAILABLE").wait_for(timeout=20000)
                assert page.locator("#sync").is_disabled()
                report.append({"scenario": "monitoring-transport-unavailable", **status(op)})
                executor.client_factory = original_factory
                completed(op, git(root / "unavailable", "rev-parse", "HEAD"))

                page.close()

                # Actual unavailable SDK transport; never falls back to the Git core.
                import httpx
                unavailable = CamundaExecutor(client_factory=lambda: sdk.CamundaClient(
                    configuration={"CAMUNDA_REST_ADDRESS": "http://127.0.0.1:1/v2", "CAMUNDA_AUTH_STRATEGY": "NONE"}, timeout=httpx.Timeout(1)))
                with served({"offline": {"path": str(root / "failure-fork")}}, unavailable) as (_, offline_url):
                    offline = browser.new_page()
                    offline.goto(offline_url)
                    offline.locator("#sync:not([disabled])").wait_for(timeout=20000)
                    head = git(root / "failure-fork", "rev-parse", "HEAD")
                    offline.locator("#sync").click()
                    offline.locator("#operation-state").filter(has_text="CREATION_UNKNOWN").wait_for(timeout=20000)
                    assert offline.locator("#sync").is_disabled()
                    response = next(iter(unavailable.operations.values()))
                    assert response["state"] == "CREATION_UNKNOWN"
                    offline.reload()
                    offline.locator("#operation-state").filter(has_text="CREATION_UNKNOWN").wait_for()
                    assert git(root / "failure-fork", "rev-parse", "HEAD") == head
                    report.append({"scenario": "camunda-unavailable", "before": head, "after": head, **response})
                    offline.close()
                finished = True
        finally:
            for child in reversed(children): stop(child)
            if not finished:
                owned = {o["process_instance_key"] for o in executor.operations.values() if o.get("process_instance_key")}
                cancel_demo_instances(client, set(keys) | owned)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camunda", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("CAMUNDA_REST_ADDRESS", "http://127.0.0.1:8080/v2")
    os.environ.setdefault("CAMUNDA_AUTH_STRATEGY", "NONE")
    root = prepare(None)
    print(f"Browser evidence directory: {root}", flush=True)
    mapping, filename = fixtures(root)
    report, errors = [], []
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            direct(browser, root, mapping, filename, report, errors)
            if args.camunda:
                with camunda_demo_lock() as lock_fd:
                    camunda(browser, root, mapping, report, errors, lock_fd)
            browser.close()
        assert not [error for error in errors if error["type"] == "pageerror"], errors
        print(f"PASS: {len(report)} scenario records; no uncaught browser exceptions", flush=True)
    finally:
        (root / "browser-report.json").write_text(json.dumps(report, indent=2) + "\n")
        (root / "browser-errors.json").write_text(json.dumps(errors, indent=2) + "\n")


if __name__ == "__main__":
    main()
