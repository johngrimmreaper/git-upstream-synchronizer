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

## Camunda integration

The Camunda BPMN model, worker, and local runtime instructions will follow as
the next development milestone. The target is the Camunda 8.9 stable line.

## Design boundaries

This project demonstrates orchestration, error handling, idempotency, and
Git safety. Production use would need access control, per-repository locking,
secrets management, structured logs, metrics, and remote push policies.
