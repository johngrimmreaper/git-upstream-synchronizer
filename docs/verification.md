# Verification record — 2026-10-24

Validation covered the synchronization core, standalone CLI, optional Camunda
worker, and executable BPMN process. The environment and integration results below
were recorded on 2026-10-24.

## Environment

- Ubuntu 26.04.1; Python 3.14.4; Git 2.53.0.
- Official Camunda Run 8.9.26, Java 21.0.12.1, H2 secondary storage.
- Official `camunda-orchestration-sdk==9.0.1`; `pytest==9.0.2`.
- VM has approximately 3.3 GiB RAM; the extracted Run distribution occupies about
  1 GB. Approximately 12 GB disk remained free after setup.
- No Docker containers/images were created for this demonstration.

## Automated checks

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

## Limits of this evidence

A missing local remote simulates a temporary fetch failure; this was not a real
external network outage. Hard crashes during Git updates, acknowledgement loss,
job-timeout redelivery, multi-host races, production authentication, resource soak,
LFS/submodule behavior, remote pushes, and disaster recovery were not demonstrated.
Worker-crash recovery is documented from Camunda semantics and core idempotency,
not presented as an observed test result.
