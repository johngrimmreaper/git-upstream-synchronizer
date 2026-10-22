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

## Surviving Git child protection — 2026-10-26

A regression test reproduced a lock-lifetime defect: killing only Python during
fetch left Git running but released the synchronizer's advisory lock. The core
now passes the held descriptor explicitly to fetch and merge with `pass_fds`.
The Git child therefore retains the lock until it exits, even after Python dies.

The full suite passed with **36 tests**; without the optional SDK, **21 tests**
passed and the worker test module was skipped. The crash test now covers four
combinations: fetch or post-merge, with only Python killed or the entire isolated
process group killed. Each verifies the branch state and eventual safe repeat.
In the parent-only cases, another synchronization is rejected while Git is alive
and accepted after its inherited lock is released.

The change is confined to core lock handling, private helpers, the existing
safety test, and these documentation updates. It adds no dependency or Camunda-specific logic.
Linux [flock lifetime](https://man7.org/linux/man-pages/man2/flock.2.html) follows
open file descriptions; closing only the Python copy must not release protection
while a mutating Git child is still running.

Both live Camunda demonstrations passed after the fix. The normal demonstration's
report is `/tmp/git-sync-demo-pmxrxhg7/report.json`; it verified all three business
outcomes, retry recovery, and incident creation. The crash report is
`/tmp/git-sync-demo-yrluh0i6/report.json`: process `2251799813735745` redelivered
job `2251799813735751`, completed as `UP_TO_DATE`, and retained all three retries.

## Interrupted checkout and linked worktrees — 2026-10-26

The full suite passed with **48 tests**. Without the optional SDK, **32 tests**
passed and the worker module was skipped. Regular-checkout regressions cover four
Git locks, partially changed file contents, and a corrupt index; all are refused
without discarding evidence or fetching. An adapter test verifies that a Git
write lock requests zero retries.

Five additional real-Git checks cover the interrupted-checkout boundary and Git
lock resolution in linked worktrees. A required smudge filter blocks checkout
after `initial.txt` changes but before the index and HEAD advance. Killing the
isolated process group leaves the partial worktree and `index.lock` in place.
A repeat call raises `SyncError` and preserves the worktree bytes, lock bytes,
and original HEAD; no repair or lock removal is attempted.

The other four checks place Git's index, HEAD, selected-branch, or packed-refs
lock in a linked worktree. Each is refused before fetching, including when the
commits are already equal. Git resolves worktree-specific and shared paths.
The checks also preserve `FETCH_HEAD`. They fail against the published baseline
and pass with the Git-lock precondition check.

The extended Camunda demonstration passed; its report is
`/tmp/git-sync-demo-wg8gl8bx/report.json`. It verified the three business outcomes,
automatic retry recovery, exhausted retries, and an immediate Git-lock incident
for process `2251799813741159`. After synthetic fixture cleanup, a new instance
completed as `UP_TO_DATE`. The crash demonstration passed with
process `2251799813740994` and job `2251799813741000`, preserving all three retries
and completing as `UP_TO_DATE`; evidence is
`/tmp/git-sync-demo-f6icuis6/report.json`. An earlier attempt could not observe the
timeout because a competing demonstration worker had already completed the job;
the repeat ran without another worker.

## Automated demonstration coordination — 2026-10-26

Both automated Camunda scripts now acquire the same nonblocking local guard
before deploying or launching a worker. Their workers inherit the descriptor.
The guard is released when the last holder exits; the remaining file is harmless.
This prevents competing temporary repository mappings within one checkout.
The core, standalone CLI, production worker, and BPMN model are unchanged.
The full suite passed with **53 tests** through the `pytest` entry point.
Without the optional SDK, **36 tests passed and 2 checks skipped**: the worker
test module and the crash-demo entry-point check.

Five tests use temporary script copies and directories. They verify rejection by
both entry points, reuse after a completed run, inheritance by a surviving child,
and standalone operation while the Camunda guard is held. They do not contact
Camunda or modify real repositories.

The live check started the crash demonstration, confirmed the guard was held,
and launched the normal demonstration concurrently. The second run exited with
code 1 and the busy message, before deployment or worker startup. The original
crash demonstration then completed: process `2251799813742078`, job
`2251799813742084`, `UP_TO_DATE`, and all three retries preserved. Its report is
`/tmp/git-sync-demo-qkrm_z9q/report.json`.

A subsequent normal demonstration passed all business outcomes, retry recovery,
exhausted retries, and the Git-lock incident. Its report is
`/tmp/git-sync-demo-h0y0extl/report.json`. The guard only coordinates automated
runs from this checkout. Manual workers, other checkouts, and other machines
still need compatible repository mappings and explicit coordination.

## Limits of this evidence

A missing local remote simulates a temporary fetch failure; this was not a real
external network outage. The crash demo stops after the Git effect but before
sending job completion. Subprocess tests cover both a Python-only crash with Git
still running and termination of the entire test process group. The checkout
test interrupts a real file update before the index and branch advance; it
verifies safe refusal afterward. Crashes during ref replacement, power loss,
and lost completion responses after Camunda accepts the command remain unverified.
A hung orphan/helper can retain the lock until it is stopped;
there is no automatic orphan reaper. Multi-host races, production authentication,
resource soak, LFS/submodule behavior, remote pushes, and disaster recovery remain
untested.
