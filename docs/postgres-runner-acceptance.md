# PostgreSQL Pull Runner Acceptance

This inventory records the pull-runner migration evidence as of 2026-10-09.
The current deployment remains in SSH/legacy mode; no scheduler deployment or
task data was changed during this acceptance work.

| Capability | Implemented | Tested | Deployed | Remaining |
| --- | --- | --- | --- | --- |
| Atomic PostgreSQL job claim and task/branch concurrency limits | Yes | Unit and ephemeral PostgreSQL transaction tests | No; pull transport is opt-in | Keep the PostgreSQL transaction job required in CI |
| Runner registration, capability report, and heartbeat | Yes | Unit tests and one-shot devMac runner against an isolated PostgreSQL schema | No LaunchAgent; one-shot only | Install and verify the Mac LaunchAgent after shared database access is approved |
| Lease expiry, attempt fencing, and guarded requeue | Yes | Unit and ephemeral PostgreSQL tests | No live lease-expiry recovery | Exercise runner loss, expiry, and stale completion with a controlled acceptance job |
| Scheduler/runner restart recovery | Partial | Unit tests ensure a running job is not claimed a second time; missing reservations fail closed | No | Test process survival/termination and workspace ownership on the Mac |
| Simplified implementation flow and shared repair budget | Yes, opt-in | Focused graph and scheduler tests | No; `WORKFLOW_MODE` defaults to legacy | Run full issue-to-PR happy path and validation, review, and CI repairs |
| Exact-revision independent final review | Yes | Gate and scheduler tests | No pull-mode E2E | Verify a complete final review and current-head CI produce READY |
| Human merge and post-merge CI reconciliation | Yes | Scheduler integration tests | Existing SSH flow | Verify in a controlled pull-runner acceptance task |

## Evidence collected

- The complete repository suite passed locally: 224 tests, with five tests
  skipped because the ordinary run did not set a PostgreSQL test URL.
- The runner-job suite passed against a disposable PostgreSQL 16 database:
  18 tests, including five tests using real PostgreSQL transactions and a worker
  subprocess.
- Pull-request CI now runs those PostgreSQL tests against its own ephemeral
  PostgreSQL service.
- On the devMac, Codex CLI authentication and quota were healthy, and both
  configured model IDs (`gpt-6-luna` and `gpt-6-sol`) returned a model probe.
- A one-shot Mac runner registered capabilities, checked quota, claimed one
  fixture job, and persisted its result through PostgreSQL. The test used a
  temporary `kubectl port-forward`, a unique schema, and a fake task subprocess;
  the schema was dropped and verified absent afterward.

## Acceptance still required

The Mac has no installed runner LaunchAgent or `runner.env`. The scheduler's
PostgreSQL service is a Kubernetes ClusterIP and is not directly reachable from
the Mac over the LAN. The one-shot test therefore proves the runner transport
and job lifecycle against the live database, but does not prove persistent
runner startup, a real Codex implementation task, GitHub PR/CI repair, crash
recovery, or post-merge completion.

Before switching deployment settings, provide an approved shared database
endpoint that the Mac can reach, install the matching runner revision and
LaunchAgent, confirm heartbeat/capabilities/quota, then run the acceptance
scenarios in [`operations.md`](operations.md). Keep one dispatcher active and
leave SSH/legacy mode enabled until those scenarios pass.
