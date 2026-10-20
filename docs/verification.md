# Verification record — 2026-10-26

Validation covered the synchronization core, standalone CLI, optional Camunda
worker, and executable BPMN process. Initial integration results were recorded on
2026-10-24; crash-recovery verification was added on 2026-10-26.

## Environment

- Ubuntu 26.04.1; Python 3.14.4; Git 2.53.0.
- Official Camunda Run 8.9.26, Java 21.0.12.1, H2 secondary storage.
- Official `camunda-orchestration-sdk==9.0.1`; `pytest==9.0.2`.
- VM has approximately 3.3 GiB RAM; the extracted Run distribution occupies about
  1 GB. Approximately 12 GB disk remained free after setup.
- No Docker containers/images were created for this demonstration.

## Automated checks — 2026-10-24

| Check | Observed result |
| --- | --- |
| Original pytest baseline | 4 passed |
| Core and CLI suite | 17 passed |
| Full suite with optional SDK | 32 passed |
| Core environment without optional SDK | 17 passed, worker test module skipped |
| Fresh temporary virtualenv, installed with no runtime dependencies | SDK import absent; installed CLI and all three standalone demo outcomes passed |
| Installer syntax and repeat installation | `bash -n` passed; existing installation reused |
| Project whitespace checks | `git diff --check` passed |
| Git commit signatures | Native OpenPGP signatures verified with `git verify-commit` |

The original four tests were retained. Additional real-Git tests cover CLI results,
invalid branch/remote configuration, detached HEAD, hidden untracked files,
unfinished operations, concurrent calls, failed fetch preservation, and preservation
of conflicting ignored files. Adapter tests cover real core calls, repository-ID
validation, retry backoff, zero-retry preconditions, unexpected exceptions, and
allowlist validation. Ordinary pytest does not contact Camunda or GitHub.

## BPMN integration results

The final run of `.venv/bin/python scripts/demo.py --camunda` completed successfully.
It deployed the checked-in model and used a separately launched Python worker.

| Scenario | Process instance key | Observed evidence |
| --- | --- | --- |
| Fast-forward | `2251799813685791` | `UPDATED`; HEAD changed; `upstream.txt` appeared; `EndUpdated` reached |
| Repeat | `2251799813685813` | `UP_TO_DATE`; same HEAD; `EndUpToDate` reached |
| Independent commits | `2251799813685830` | `NEEDS_REVIEW`; both histories preserved; `EndNeedsReview` reached |
| Temporary failure | `2251799813685848` | First fetch failed with 2 retries remaining; restoring the remote let the same instance complete as `UPDATED` |
| Persistent failure | `2251799813685874` | Retries exhausted; active incident `2251799813685899`; HEAD unchanged; no success status variable |

The run produced `/tmp/git-sync-demo-y63ckpqo/report.json` and
`/tmp/git-sync-demo-y63ckpqo/worker.log` on the test machine. These temporary files
are not permanent project artifacts; rerunning the script recreates equivalent
evidence with new keys and commit IDs. At the end of the run, the worker log
recorded clean shutdown and the final incident remained active in Operate.

`ss -ltn` confirmed loopback bindings for ports 8080, 8086, 9600, 26500, 26501,
and 26502. The core REST API was exercised directly; BPMN branch verification used
the element-instance search API. The Operate screen and Desktop Modeler GUI were
not visually inspected. The bundled Connectors are unused by this BPMN process;
their health endpoint reported `UP`, including the Camunda client and
process-definition importer.
The manual README procedure was also run through the installed console entry
point: process `2251799813685970` completed and its Git file change was verified.

## Crash recovery — 2026-10-26

The full pytest suite passed with **34 tests**. Without the optional SDK, the core
and CLI suite passed with **19 tests** and the worker module was skipped. Two new
subprocess tests kill an isolated synchronization process group during a blocked
fetch or in a post-merge hook after the fast-forward. A repeated call returns
`UPDATED` or `UP_TO_DATE`, respectively, and the existing advisory lock file does
not prevent recovery after the process exits.

The live `.venv/bin/python scripts/demo_crash_recovery.py` verification passed:

| Check | Observed result |
| --- | --- |
| Process instance | `2251799813734845` |
| Job key before and after redelivery | `2251799813734851` |
| First execution | `UPDATED`; upstream file appeared and HEAD advanced |
| Abrupt exit | Fault worker called `os._exit(23)` before SDK job completion |
| Activation timeout | `TIMED_OUT`; retries remained 3; no result status had reached the process |
| Replacement | Normal worker completed the same job as `UP_TO_DATE` |
| Git after redelivery | HEAD and commit count unchanged |
| Process outcome | `EndUpToDate`; no incident; retries remained 3 |

The fault worker used a 15-second activation timeout. The normal worker and core
were unchanged. Evidence was written to `/tmp/git-sync-demo-c3dtf8u8/report.json`,
`before-crash.json`, `crash-worker.log`, and `recovery-worker.log` in that directory.
The fault worker's SDK resource-tracker warning is expected after abrupt exit;
the replacement worker shut down normally.

The original Camunda demonstration also passed again on 2026-10-26: all three
business outcomes, temporary fetch failure/recovery, and an exhausted-retry
incident were verified. Its report is `/tmp/git-sync-demo-uqpo1zb7/report.json`.

## Limits of this evidence

A missing local remote simulates a temporary fetch failure; this was not a real
external network outage. The crash demo stops after the Git effect but before
sending job completion. The subprocess tests kill the full test process group,
including its Git children. They do not verify crashes inside checkout/ref writes,
a Python-only crash that leaves Git running, or lost completion responses after
Camunda accepts the command. Multi-host races, production authentication, resource
soak, LFS/submodule behavior, remote pushes, and disaster recovery remain untested.
