# Local integration testing

Run all commands from the repository root on Linux. Tests only use disposable Git
repositories. The ordinary pytest suite needs no running Camunda server.

## Unit, safety and browser tooling

```bash
.venv/bin/python -m pip install -e '.[dev,camunda,browser]' build
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
node --check src/git_upstream_synchronizer/web_assets/app.js
git diff --check
.venv/bin/python scripts/test_browser_integration.py
```

`browser` is a development/testing extra, separate from application runtime
requirements. Chromium is installed on the test machine. The browser harness starts
real loopback HTTP servers and inspects their HTML/JavaScript through Chromium.
It verifies literal HTML-looking metadata and diffs, a 390px layout, refresh/error
recovery, direct synchronization, and ahead/diverged selection behavior.

## Real Camunda tests

Start the native pinned c8run runtime as described in the README. Verify
`curl --fail http://127.0.0.1:8080/v2/topology`. Stop manual workers/supervisors before
running these sequential commands; workers for this task type must share the same
repository-ID mapping.

```bash
export CAMUNDA_REST_ADDRESS=http://127.0.0.1:8080/v2
export CAMUNDA_AUTH_STRATEGY=NONE
.venv/bin/python scripts/demo.py --camunda
.venv/bin/python scripts/demo_crash_recovery.py
.venv/bin/python scripts/test_browser_integration.py --camunda
```

The browser harness holds `infra/camunda/demonstration.lock`, passes the guard to
its worker, uses unique per-run repository IDs, and refuses to start when an
eligible job already exists. This coordinates this checkout, not other machines
or independently launched workers. The native API is eventually consistent:
inspect pending work and workers before any test. Old zero-retry incidents remain
operator evidence and are not automatically canceled or resolved.

The browser command checks the complete browser → Python HTTP → BPMN → worker →
Git → end event → browser flow. It verifies `UPDATED`, HTTP idempotency
`UP_TO_DATE`, stale previews and ahead/diverged `NEEDS_REVIEW`, preserved dirty
files and Git locks, same-process automatic fetch retries, persistent incidents,
and operator recovery of the original job. It checks process keys as strings,
Operate links, query-failure recovery, and unavailable-Camunda creation without
fallback. The first browser-started process uses the existing fault worker:
it exits after the Git effect but before acknowledgment. The harness observes
`TIMED_OUT` with three retries, then a normal replacement completes the same job
as `UP_TO_DATE` without another HEAD or commit-count change. The separate crash
script independently checks the same recovery semantics.

Each command prints a new `/tmp/git-sync-demo-*` directory. Browser evidence is
`browser-report.json`, `browser-errors.json`, `web-worker.log`, and PNG screenshots.
Expected HTTP503 console errors from injected failures remain in the error report;
uncaught page exceptions fail the test. Inspect screenshots after a successful run.
Failures stop owned workers/servers and attempt cancellation only of the run's
recorded process instances, preserving the original error. SIGKILL or an ambiguous
creation response may still require manual investigation.

## Installed wheel verification

```bash
.venv/bin/python -m build --wheel
.venv/bin/python scripts/verify_wheel.py dist/git_upstream_synchronizer-0.1.0-py3-none-any.whl
.venv/bin/python scripts/verify_wheel.py dist/git_upstream_synchronizer-0.1.0-py3-none-any.whl --camunda
```

Each command creates a fresh environment, unsets `PYTHONPATH`, runs outside the
checkout, and verifies imports from `site-packages`. It exercises installed console
entry points and served wheel assets. The SDK-free case checks direct operation
and a useful missing-extra startup error. The optional case installs the declared
Camunda extra, deploys the model, and executes the installed web/worker path.

To run the ordinary suite against an installed SDK-free wheel, install pytest in
that fresh environment and use `env -u PYTHONPATH /path/to/venv/bin/python -m pytest
-q -o pythonpath=`. The project's default pytest `pythonpath=src` must be disabled
for this installation check.

## Supervised review stack

With c8run healthy, create a new disposable demonstration:

```bash
.venv/bin/python scripts/serve_demo.py --directory /tmp/git-sync-review-20261030
```

The supervisor runs one compatible worker and the Camunda-mode web server on
127.0.0.1:8089, holds the demonstration guard, and writes `stack.json`,
`web-repos.json`, `worker.log`, and `web.log` inside that directory. `ready` can
fast-forward, `review` has independent commits, and `smoke` provides a second
behind fixture. Ctrl+C or SIGTERM to the supervisor stops only its children.
It leaves Camunda and all Git fixture contents available. Resume the same mapping:

```bash
.venv/bin/python scripts/serve_demo.py --directory /tmp/git-sync-review-20261030 --resume
```

After stopping, inspect pending processes before resuming: the supervisor refuses
eligible outstanding jobs rather than guessing an old mapping. No Git lock is
removed automatically. There is no exactly-once or Git/Camunda transaction claim.

On the desktop:

```bash
ssh -N \
  -L 8089:127.0.0.1:8089 \
  -L 8080:127.0.0.1:8080 \
  codex@codexvm
```

Open http://127.0.0.1:8089 and http://127.0.0.1:8080/operate. Local Operate credentials
are `demo` / `demo`. Keep both listeners on loopback.
