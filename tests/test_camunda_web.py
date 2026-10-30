"""Optional adapter tests with the pinned SDK models and no live server."""
from types import SimpleNamespace as NS
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("camunda_orchestration_sdk")
from git_upstream_synchronizer.camunda_web import CamundaExecutor, OperationNotFound
from test_web import running_server
from test_sync import repositories


def state(value):
    return NS(value=value)


class Client:
    def __init__(self):
        self.creations = []
        self.processes = []
        self.jobs = []
        self.incidents = []
        self.variables = []
        self.ends = []
        self.fail_creation = False
        self.fail_monitoring = False

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def create_process_instance(self, *, data):
        self.creations.append(data.to_dict())
        if self.fail_creation:
            raise TimeoutError("sensitive URL and credentials")
        return NS(process_instance_key="22517998136852499")

    def search_process_instances(self, *, data):
        assert data.to_dict()["filter"]["processInstanceKey"] == "22517998136852499"
        if self.fail_monitoring:
            raise ConnectionError("sensitive details")
        return NS(items=self.processes)

    def search_jobs(self, **kwargs): return NS(items=self.jobs)
    def search_incidents(self, **kwargs): return NS(items=self.incidents)
    def search_variables(self, **kwargs): return NS(items=self.variables)
    def search_element_instances(self, **kwargs): return NS(items=self.ends)


def adapter():
    client = Client()
    return CamundaExecutor(client_factory=lambda: client), client


def poll(executor, operation):
    executor.operations[operation["operation_id"]]["_polled"] = 0
    return executor.status(operation["operation_id"], "demo")


def test_submit_serializes_sdk_request_and_coalesces_concurrent_calls():
    executor, client = adapter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: executor.submit("demo"), range(12)))
    assert len({r["operation_id"] for r in results}) == 1
    assert len(client.creations) == 1
    request = client.creations[0]
    assert request["processDefinitionId"] == "git-upstream-sync"
    assert request["variables"]["repository_id"] == "demo"
    assert results[0]["process_instance_key"] == "22517998136852499"
    assert results[0]["operate_url"].endswith("/processes/22517998136852499")
    assert "result" not in results[0]


def test_ambiguous_creation_never_retries_or_falls_back():
    executor, client = adapter()
    client.fail_creation = True
    operation = executor.submit("demo")
    assert operation["state"] == "CREATION_UNKNOWN"
    assert "sensitive" not in str(operation)
    assert executor.submit("demo") == operation
    assert poll(executor, operation) == operation
    assert len(client.creations) == 1


def test_eventual_visibility_monitoring_outage_and_retry_incident_recovery():
    executor, client = adapter()
    operation = executor.submit("demo")
    assert poll(executor, operation)["state"] == "PENDING"
    client.fail_monitoring = True
    assert poll(executor, operation)["state"] == "UNAVAILABLE"
    client.fail_monitoring = False
    client.processes = [NS(state=state("ACTIVE"))]
    client.jobs = [NS(job_key="9007199254740993", retries=2, state=state("FAILED"))]
    result = poll(executor, operation)
    assert result["state"] == "RETRYING" and result["remaining_retries"] == 2
    assert result["job_key"] == "9007199254740993"
    client.incidents = [NS(incident_key="9007199254740995", state=state("ACTIVE"))]
    assert poll(executor, operation)["state"] == "INCIDENT"
    client.incidents = []
    client.jobs[0].state = state("ACTIVATED")
    assert poll(executor, operation)["state"] == "RUNNING"
    client.processes[0].state = state("COMPLETED")
    assert poll(executor, operation)["state"] == "PENDING"
    client.variables = [NS(name=n, value='"' + v + '"') for n, v in
        {"status": "UPDATED", "previous_sha": "a"*40, "upstream_sha": "b"*40, "current_sha": "b"*40}.items()]
    assert poll(executor, operation)["state"] == "PENDING"
    client.ends = [NS(state=state("COMPLETED"))]
    client.jobs[0].state = state("COMPLETED")
    client.jobs[0].retries = 3
    result = poll(executor, operation)
    assert result["state"] == "COMPLETED" and result["end_event"] == "EndUpdated"
    assert result["result"]["status"] == "UPDATED"
    assert result["remaining_retries"] == 3 and result["job_state"] == "COMPLETED"
    assert len(client.creations) == 1


def test_status_is_scoped_and_lookup_rate_is_bounded():
    executor, client = adapter()
    operation = executor.submit("demo")
    with pytest.raises(OperationNotFound): executor.status("unknown", "demo")
    with pytest.raises(OperationNotFound): executor.status(operation["operation_id"], "other")
    assert poll(executor, operation)["state"] == "PENDING"
    client.fail_monitoring = True
    assert executor.status(operation["operation_id"], "demo")["state"] == "PENDING"


def test_http_camunda_creation_and_scoped_monitoring(repositories):
    _, fork = repositories
    executor, client = adapter()
    with running_server(fork, executor) as (_, request):
        status, operation = request("/api/sync", {"repository_id": "demo"})
        assert status == 202 and operation["state"] == "PENDING"
        assert request("/api/operation", {"repository_id": "demo", "operation_id": "foreign"})[0] == 404
        status, result = request("/api/operation", {"repository_id": "demo", "operation_id": operation["operation_id"]})
        assert status == 200 and result["state"] == "PENDING"
        assert not (fork / ".git/FETCH_HEAD").exists()


def test_unsafe_operate_url_is_rejected():
    with pytest.raises(ValueError): CamundaExecutor(operate_url="javascript:alert(1)")
