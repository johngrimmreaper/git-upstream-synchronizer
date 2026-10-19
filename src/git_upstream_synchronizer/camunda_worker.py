"""Optional Camunda adapter. No Camunda types cross into the core."""

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import signal

from camunda_orchestration_sdk import CamundaAsyncClient, JobFailure, SyncJobContext, WorkerConfig

from .sync import GitCommandError, SyncError, synchronize_repository

LOG = logging.getLogger(__name__)
JOB_TYPE = "synchronize-repository"
RETRY_BACKOFF_MS = 5_000


def handle_job(job: SyncJobContext, repositories: dict[str, dict[str, str]]) -> dict[str, object]:
    """Translate a trusted repository ID into a core call and map failures."""
    try:
        repository_id = job.variables.to_dict().get("repository_id")
        if not isinstance(repository_id, str) or repository_id not in repositories:
            raise SyncError("repository_id must name an entry in the worker configuration")
        config = repositories[repository_id]
        LOG.info("job=%s repository=%s retries=%s", job.job_key, repository_id, job.retries)
        result = synchronize_repository(
            Path(config["path"]), branch=config.get("branch", "main"),
            upstream=config.get("upstream", "upstream"),
        )
    except SyncError as error:
        # An operator must fix invalid input/unsafe state; automatic retries cannot help.
        raise JobFailure(message=str(error), retries=0) from error
    except GitCommandError as error:
        # Preserve failure semantics; Camunda owns retries and creates the incident.
        LOG.warning("job=%s Git operation failed; requesting retry", job.job_key)
        raise JobFailure(message=str(error), retry_back_off=RETRY_BACKOFF_MS) from error
    LOG.info("job=%s status=%s current_sha=%s", job.job_key, result.status, result.current_sha)
    return asdict(result)


def load_repositories(path: Path) -> dict[str, dict[str, str]]:
    """Load a small local allowlist; process variables cannot select arbitrary paths."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("Repository configuration must be a nonempty JSON object")
    for name, config in data.items():
        if not isinstance(config, dict) or set(config) - {"path", "branch", "upstream"}:
            raise ValueError(f"Invalid configuration for {name!r}")
        if not config.get("path") or not all(isinstance(v, str) and v for v in config.values()):
            raise ValueError(f"Configuration for {name!r} needs a path and nonempty strings")
        if not Path(config["path"]).is_absolute():
            raise ValueError(f"Repository path for {name!r} must be absolute")
    return data


async def run_worker(repositories: dict[str, dict[str, str]]) -> None:
    async with CamundaAsyncClient(logger=LOG) as client:
        client.create_job_worker(
            config=WorkerConfig(
                job_type=JOB_TYPE, job_timeout_milliseconds=600_000,
                max_concurrent_jobs=1, fetch_variables=["repository_id"],
                worker_name="git-upstream-synchronizer",
            ),
            callback=lambda job: handle_job(job, repositories),
            execution_strategy="thread",
        )
        await client.run_workers()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the optional Camunda Git worker")
    parser.add_argument("--repositories", type=Path, required=True, help="Local JSON allowlist")
    args = parser.parse_args(argv)
    try:
        repositories = load_repositories(args.repositories)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(run_worker(repositories))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
