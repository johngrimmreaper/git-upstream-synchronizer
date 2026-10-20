# Architecture and design rationale

## 1. Why Python and an external process worker?

Python keeps the synchronization code short and readable. The installed Git CLI
already implements fetching, ancestry checks, and safe fast-forward updates, so
there is no need to recreate Git operations with another library. Argument lists
are passed directly to subprocesses without a shell.

The external worker is a separate process connected to Camunda through its official
Python SDK. It demonstrates the exercise's job-worker mechanism while keeping
Camunda imports and job types outside the core. The CLI is the practical proof:
it calls the same function with no SDK installed. A different backend would only
need to translate its input into a path, branch, and remote and handle `SyncResult`
or exceptions. There is no need for a generic interface or dependency container.

The SDK needs an asyncio polling loop, but Git is blocking. A synchronous handler
runs in the SDK's thread strategy with one job at a time. Asynchronous code stops
at the adapter boundary; no async Git engine was added.

## 2. Why this BPMN model?

The process contains a Start Event, one Service Task, one Exclusive Gateway, and
three named End Events. A synchronization request creates a `synchronize-repository`
job. Its worker returns `status` and the before/upstream/after commit IDs.

The gateway evaluates three FEEL conditions: `UPDATED`, `UP_TO_DATE`, and
`NEEDS_REVIEW`. The model makes the business outcome visible in Operate without
modeling individual Git commands as separate tasks. Keeping validation, fetch,
and update together also keeps the repository lock within one worker invocation.

There is no approval workflow: the review end event reports that an operator needs
to decide what to do. There is no default route; an unknown result should create
an incident. Three branches are useful for explaining the exercise even though a
standalone synchronizer would simply print its result.

## 3. How are errors, retries, and unexpected worker failures handled?

Divergence is an expected business result, not a technical failure. The branch
stays unchanged, and the process completes at `NEEDS_REVIEW`.

Failed Git commands raise `GitCommandError`. The adapter requests job failure
with a five-second backoff. The SDK decrements Camunda's remaining retries, starting
at three; the third failed attempt produces an incident. Unsafe preconditions raise
`SyncError` and fail with zero retries because operator intervention is needed.
Unexpected exceptions are not swallowed: the SDK fails the job with its default
retry behavior. These cases are covered by focused adapter tests.

A dead worker cannot acknowledge completion. After the ten-minute activation
timeout Camunda can deliver the job again without reducing retries merely because
of the timeout. If Git was already updated, the repeat call returns `UP_TO_DATE`.
This is at-least-once job handling with an idempotent Git effect, not exactly-once
execution or a distributed transaction. The advisory lock prevents overlapping
calls from this tool; it cannot coordinate unrelated Git applications.

The demonstrations verify temporary fetch failure/recovery, exhausted-retry
incidents, and an abrupt worker exit after Git updates but before job completion.
The crash demo uses the production handler, a 15-second activation timeout, and
then the normal replacement worker. Camunda records the timeout without consuming
retries; the same job completes as `UP_TO_DATE` without another Git change.

Separate core tests send `SIGKILL` to an isolated process group while Git is blocked
during fetch or in a post-merge hook after the fast-forward. Both verify that the
repository lock is released and another invocation can proceed. A crash inside
checkout/ref writes, a surviving orphan Git process, and a lost completion response
after Camunda accepts the command remain outside the verified scope.

## 4. Which architectural trade-offs were considered?

- **Git CLI rather than a Git library:** reuses familiar, installed Git behavior
  and keeps dependencies small. It requires subprocess timeouts and trusted local
  repository configuration.
- **One function rather than a framework:** the core accepts ordinary Python
  values and returns a dataclass. Two thin adapters solve the actual portability
  requirement; a plugin system would add concepts without enabling the demo.
- **Local fast-forward only:** the tool cannot silently resolve divergence or
  overwrite history. It also does not publish local updates back to a hosting service.
- **Camunda Run rather than Compose:** both have a lightweight H2 option. Run avoids
  images and filesystem mounts on this small VM; it requires a Java runtime. The
  bundled Connectors are unused by this process.
- **Local allowlist rather than paths in process variables:** a process starter
  selects a known repository ID. It cannot direct the worker to arbitrary paths or
  change a Git remote through BPMN variables. The configuration stays a small JSON file.
- **Camunda rather than cron:** cron is sufficient for simple periodic synchronization.
  Camunda earns its operational/licensing cost when a larger process needs durable
  orchestration, visibility, retries, or incidents. The application can outlive the backend.

## 5. What would change for production?

First define the real operating scope: one repository host, many repositories, or
multiple worker machines. Choose durable checkout ownership and locking appropriate
to that scope, enforce Git credentials with minimum privileges, and prevent manual
writers from racing synchronization. Decide explicitly whether pushes and upstream
signature checks are required; neither is implemented.

Protect the Camunda API with authentication and authorization, run the worker with
limited filesystem access, and keep secrets out of variables, remotes, and logs.
Use supported production storage/runtime deployment, backups, resource limits,
incident ownership, log retention, and monitoring. Extend fault tests to partial
Git writes, orphan Git processes, and loss of completion responses. Review Camunda's commercial
production licensing. Camunda Run/H2 and the current demonstration are insufficient
as a production platform.

None of that requires adding Camunda types to the synchronization engine. If the
actual requirement remains a scheduled local fast-forward, use the CLI and an OS
timer instead of operating a workflow engine.

## Code walkthrough

| Component | Responsibility |
| --- | --- |
| `src/git_upstream_synchronizer/sync.py` | Check repository state, fetch upstream, compare ancestry, and apply a safe fast-forward under an advisory lock |
| `src/git_upstream_synchronizer/cli.py` | Parse arguments, invoke the core, and return JSON with a corresponding exit code |
| `src/git_upstream_synchronizer/camunda_worker.py` | Resolve a repository ID through local configuration, call the core, and translate results or exceptions into Camunda job handling |
| `bpmn/synchronize-repository.bpmn` | Dispatch the Service Task and route its result to one of three End Events |
| `scripts/demo.py` | Create isolated Git repositories and verify synchronization outcomes and Camunda retry behavior |
| `scripts/demo_crash_recovery.py` | Verify job timeout redelivery after an abrupt exit between the Git update and Camunda completion |

The central Git decision compares the local and fetched commit IDs. Equal commits
require no update. An ancestry check permits a fast-forward only when local HEAD
is an ancestor of upstream. Local-ahead, divergent, and unrelated histories return
`NEEDS_REVIEW` without changing the local branch.

The worker translates `repository_id` into the same core call used by the CLI.
The returned status determines the BPMN route. A failed fetch instead raises an
exception and follows Camunda's retry and incident mechanisms.
