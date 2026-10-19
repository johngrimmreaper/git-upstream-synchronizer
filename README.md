# Git Upstream Synchronizer

A small, interview-focused proof of concept for safely synchronizing a local Git
fork with an upstream repository. Camunda 8 will orchestrate the workflow using
BPMN 2.0 and an external Python job worker.

## Scope

The synchronization engine only performs local fast-forward updates. It does
**not** push changes to GitHub, force-update branches, or auto-merge divergences.

### Outcomes

- `UPDATED`: the local branch was fast-forwarded to the upstream commit.
- `UP_TO_DATE`: the local branch already matches the upstream commit.
- `NEEDS_REVIEW`: the histories have diverged or the local branch is ahead.

Technical failures, such as a missing remote or network timeout, raise an
exception rather than pretending the synchronization succeeded.

## Local tests

Requires Python 3.11+, Git, and pytest.

```bash
python3 -m pytest -q
```

The tests create isolated temporary Git repositories and never access GitHub.

## Standalone CLI

The core and CLI require no Camunda packages or Docker. Install with `pip install .`
inside a virtual environment, then run:

```bash
git-upstream-sync /path/to/local/fork --branch main --upstream upstream
```

The configured remote must already exist. Output is JSON containing `status`,
`previous_sha`, `upstream_sha`, and `current_sha`. Exit codes are 0 for an update
or equality, 2 for `NEEDS_REVIEW`, and 1 for technical/precondition failures
(error JSON on stderr). Argument parsing errors also use exit code 2.

Synchronization refuses dirty/detached worktrees, unfinished Git operations,
invalid branch/remote names, and overlapping calls from this tool. An advisory
Linux lock does not prevent other Git tools from writing: use a dedicated checkout.
Ignored files that conflict with an incoming tracked file are preserved.

## Camunda integration

The optional adapter uses the official `camunda-orchestration-sdk==9.0.1` with
Camunda Run **8.9.26**. SDK major 9 matches server 8.9. All Camunda imports stay
in the worker and the optional demonstration backend.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,camunda]'
sudo apt-get install --no-install-recommends openjdk-21-jre-headless
bash scripts/install-camunda.sh
```

Start the official runtime in a terminal (the relative config path is necessary
for this version of its launcher):

```bash
cd infra/camunda/c8run-8.9.26
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export SERVER_ADDRESS=127.0.0.1
export JAVA_OPTS='-Xms128m -Xmx768m -XX:ActiveProcessorCount=2'
./c8run start --config ../../application.yaml
```

The checked-in configuration binds development services to loopback. Do not
set `MANAGEMENT_SERVER_ADDRESS` globally: the bundled Connectors runtime uses
its main HTTP port for management. The core's separate management listener is
bound in `infra/application.yaml` instead.

From the project root in another terminal, run the reproducible scenarios:

```bash
.venv/bin/python scripts/demo.py --camunda
```

This deploys `bpmn/synchronize-repository.bpmn`, launches an external worker,
and asserts three result paths, temporary fetch failure/recovery, and an incident
when retries are exhausted. Each run creates fresh isolated Git repositories;
logs and `report.json` remain in the printed temporary directory. The script
stops its worker and leaves the runtime and final incident available in Operate
at <http://127.0.0.1:8080/operate> (local development login: `demo` / `demo`).

The standalone equivalent is `.venv/bin/python scripts/demo.py` and works
without the SDK installed. `--prepare` creates only fixtures for manual use.
Stop the runtime from its directory with `./c8run stop`.

## Design boundaries

This project demonstrates orchestration, error handling, idempotency, and
Git safety. Production use would need access control, per-repository locking,
secrets management, structured logs, metrics, and remote push policies.
