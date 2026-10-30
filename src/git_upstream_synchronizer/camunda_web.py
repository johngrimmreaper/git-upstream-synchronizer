"""Optional SDK boundary for web operations; Camunda alone owns job retries."""

import json
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit

# This module is imported only when --execution=camunda is selected.
try:
    import camunda_orchestration_sdk as sdk
    import httpx
except ImportError as error:
    raise ImportError("Camunda web mode requires git-upstream-synchronizer[camunda]") from error

LOG = logging.getLogger(__name__)
ENDS = {"UPDATED": "EndUpdated", "UP_TO_DATE": "EndUpToDate", "NEEDS_REVIEW": "EndNeedsReview"}
TERMINAL = {"COMPLETED", "CANCELED", "REJECTED"}


class OperationNotFound(ValueError):
    """The operation was not created by this server."""


class OperationCapacityError(RuntimeError):
    """The local in-memory operation journal is full."""


class CamundaExecutor:
    mode = "camunda"

    def __init__(self, process_id="git-upstream-sync", operate_url="http://127.0.0.1:8080/operate",
                 client_factory=None):
        parsed = urlsplit(operate_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Operate URL must be an http(s) base URL without query or fragment")
        self.process_id = process_id
        self.operate_url = operate_url.rstrip("/")
        # Per-call clients avoid sharing an SDK transport between request threads.
        # The verified SDK create method sends once; no application creation retry.
        self.client_factory = client_factory or (lambda: sdk.CamundaClient(timeout=httpx.Timeout(3)))
        self.operations = {}
        self.lock = threading.Lock()

    def _public(self, operation):
        return {key: value for key, value in operation.items() if not key.startswith("_")}

    def submit(self, repository_id):
        with self.lock:
            for operation in self.operations.values():
                if operation["repository_id"] == repository_id and operation["state"] not in TERMINAL:
                    return self._public(operation)
            if len(self.operations) >= 256:
                raise OperationCapacityError("Operation journal is full; restart after reviewing tracked processes")
            operation_id = secrets.token_urlsafe(24)
            operation = {"operation_id": operation_id, "repository_id": repository_id,
                         "execution": self.mode, "state": "PENDING", "process_instance_key": None,
                         "message": "Waiting for process visibility"}
            self.operations[operation_id] = operation
            try:
                with self.client_factory() as client:
                    result = client.create_process_instance(data=sdk.ProcessCreationById(
                        process_definition_id=self.process_id, variables=
                        sdk.ProcessInstanceCreationInstructionByIdVariables.from_dict(
                            {"repository_id": repository_id, "web_operation_id": operation_id})))
                key = str(result.process_instance_key)
                operation.update(process_instance_key=key,
                                 operate_url=f"{self.operate_url}/processes/{key}")
            except Exception:
                # A lost response can follow an accepted creation. Retain the reservation.
                LOG.exception("Process creation not acknowledged: operation=%s repository=%s",
                              operation_id, repository_id)
                operation.update(state="CREATION_UNKNOWN", message=
                    "Camunda did not acknowledge creation. It may have accepted the process. "
                    "Inspect Operate using web_operation_id; do not resubmit automatically.")
            return self._public(operation)

    def status(self, operation_id, repository_id):
        with self.lock:
            operation = self.operations.get(operation_id)
            if operation is None or operation["repository_id"] != repository_id:
                raise OperationNotFound("Unknown operation for this repository")
            if operation["state"] in TERMINAL or operation["state"] == "CREATION_UNKNOWN":
                return self._public(operation)
            if time.monotonic() - operation.get("_polled", 0) < .5:
                return self._public(operation)
            operation["_polled"] = time.monotonic()
            key = operation["process_instance_key"]
            try:
                with self.client_factory() as client:
                    self._refresh(client, key, operation)
            except Exception:
                LOG.exception("Camunda monitoring unavailable: operation=%s process=%s", operation_id, key)
                operation.update(state="UNAVAILABLE", message=
                    "Camunda monitoring is unavailable. Workflow outcome is unknown; resume monitoring.")
            return self._public(operation)

    def _refresh(self, client, key, operation):
        items = client.search_process_instances(data=sdk.ProcessInstanceSearchQuery(
            filter_=sdk.ProcessInstanceSearchQueryFilter(process_instance_key=key))).items
        if not items:
            operation.update(state="PENDING", message="Waiting for process visibility in Camunda")
            return
        process_state = items[0].state.value
        if process_state == "COMPLETED":
            values = client.search_variables(data=sdk.VariableSearchQuery(
                filter_=sdk.VariableSearchQueryFilter(process_instance_key=key)), truncate_values=False).items
            result = {item.name: json.loads(item.value) for item in values
                      if item.name in {"status", "previous_sha", "upstream_sha", "current_sha"}}
            end = ENDS.get(result.get("status"))
            if end and all(isinstance(result.get(n), str) and len(result[n]) in {40, 64}
                           and all(c in "0123456789abcdef" for c in result[n])
                           for n in ("previous_sha", "upstream_sha", "current_sha")):
                ends = client.search_element_instances(data=sdk.ElementInstanceSearchQuery(
                    filter_=sdk.ElementInstanceSearchQueryFilter(process_instance_key=key, element_id=end))).items
                if any(item.state.value == "COMPLETED" for item in ends):
                    jobs = client.search_jobs(data=sdk.JobSearchQuery(
                        filter_=sdk.JobSearchQueryFilter(process_instance_key=key))).items
                    completed_job = next((job for job in jobs if job.state.value == "COMPLETED"), None)
                    if completed_job:
                        operation.update(job_key=str(completed_job.job_key),
                                         remaining_retries=completed_job.retries, job_state="COMPLETED")
                    operation.pop("incident_key", None)
                    operation.update(state="COMPLETED", result=result, end_event=end,
                                     message="Workflow completed: " + result["status"])
                    return
            operation.update(state="PENDING", message="Process completed; waiting for result and end-event visibility")
            return
        if process_state in {"CANCELED", "TERMINATED"}:
            operation.update(state="CANCELED", message="Workflow canceled; inspect Git before another request")
            return
        incidents = client.search_incidents(data=sdk.IncidentSearchQuery(
            filter_=sdk.IncidentSearchQueryFilter(process_instance_key=key))).items
        jobs = client.search_jobs(data=sdk.JobSearchQuery(
            filter_=sdk.JobSearchQueryFilter(process_instance_key=key))).items
        active = next((item for item in incidents if item.state.value == "ACTIVE"), None)
        job = jobs[0] if jobs else None
        if job:
            operation.update(job_key=str(job.job_key), remaining_retries=job.retries,
                             job_state=job.state.value)
        if active:
            operation.update(state="INCIDENT", incident_key=str(active.incident_key),
                             message="Workflow incident requires operator attention in Operate")
        elif job and job.state.value in {"FAILED", "TIMED_OUT"}:
            operation.update(state="RETRYING", message=
                f"Waiting for Camunda redelivery; {job.retries} retries remaining")
            operation.pop("incident_key", None)
        else:
            operation.update(state="RUNNING", message="Camunda workflow is running")
            operation.pop("incident_key", None)
