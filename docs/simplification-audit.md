# Orchestrator Simplification Audit

**Audit date:** 2026-10-08
**Scope:** Current repository source, tests, deployment manifests, and the live
task lifecycle records available from the scheduler database. This is a
migration baseline, not a description of the target architecture.

## Current flow

1. The scheduler polls enabled repositories for issues with the configured
   ready label, validates their issue body, records a task, and removes the
   label. Direct prompts are also stored as tasks.
2. The scheduler claims queued tasks in PostgreSQL and starts a worker on the
   physical Mac by synchronous SSH. It keeps the SSH process open while the
   Mac runs the task; final PR review is also dispatched over SSH.
3. The graph loads the issue, plans ordered steps, prepares a workspace and Dev
   Container, then runs a coder, deterministic validation, and reviewer for
   each step. It performs final validation/review and publishes a branch and
   draft PR. LangGraph state is checkpointed to PostgreSQL in k3s and SQLite
   for local development.
4. The scheduler polls CI, dispatches bounded CI repairs, performs another
   fresh PR review, marks passing tasks READY, detects human approval, and
   currently merges approved PRs into the configured development branch. It
   later opens a development-to-release PR and records DONE after merge and
   post-merge CI.

## Component decisions

| Component | Current responsibility | Decision | Replacement |
| --- | --- | --- | --- |
| `app/graph.py` | 2,753-line LangGraph with planning, per-step coding/review, retries, checkpoint commits, final review, push and PR creation | Simplify after migration | One runner-owned implementation workflow, deterministic validation, one final independent review, and explicit phase reporting |
| `app/state.py` | Broad typed state for graph checkpoints, including per-step attempts, side-effect history, CI, review, and environment state | Reduce after active checkpoints are drained | Durable task/job records plus small transient worker input/output models |
| `app/tasks.py` `TaskStore` | SQLite/PostgreSQL task, event, activity, repository, service-health storage; status checks and per-task attempt fields | Keep and extend | Six lifecycle states; add PostgreSQL jobs, attempts, runner registration/leases, and stable task events before migrating callers |
| LangGraph `PostgresSaver` / `SqliteSaver` | Durable graph-node checkpoints | Keep during compatibility period | Job attempts and task events provide restart evidence; remove checkpoint dependency only after old workflows are complete or migrated |
| `app/task_scheduler.py` | Issue intake, synchronous SSH worker lifecycle, queue claims, CI polling/repair, final review, READY checks, merge detection/merge, release promotion, notifications | Split and simplify | Bounded reconciliation passes; PostgreSQL job dispatch/status reconciliation; GitHub writes and CI monitoring remain in the control plane |
| `scripts/mac_ssh_entrypoint.py` | SSH forced commands for health, worker execution, review, and probe; current working tree also contains an uncommitted asynchronous-submit extension | Keep transport until pull runner is proven; rename concepts generically | Managed runner daemon pulling PostgreSQL jobs; remove SSH execution only after rollout and recovery tests |
| `app/runner_jobs.py`, `app/review_worker.py` | Untracked working-tree WIP for a Mac-side file-backed job spool, child process supervision, and review execution | Do not treat as the target job store | Reuse only safe process supervision; replace local JSON job authority with shared PostgreSQL claims, attempts, leases, and fenced results |
| `app/workspace.py` | Isolated per-task clone/branch, safe Git checks, commits, pushes, checkpoint finalization/history rewrite | Keep isolation and Git guards; simplify checkpoint mechanics later | Reconstructible workspace keyed by repository/task/branch; ordinary task commits and no history rewrite |
| `app/retry_isolation.py` | Captures failed patches and resets to attempt baselines | Keep until repair semantics are migrated | Shared task repair budget with preserved artifacts and branch history |
| `app/side_effects.py` | Stable operation IDs and write-ahead intent/reconciliation for Git push, draft PR, issue comments, checkpoint and finalization | Keep the idempotency guarantees; simplify the abstraction | Stable deduplication IDs and an outbox only where DB and external effects need coordinated recovery |
| `app/github_client.py` | GitHub App auth, issue/label/comment operations, draft PRs, CI, approvals, merging, and release-promotion PRs | Keep control-plane GitHub ownership; remove automatic merge from target flow | Scheduler creates/updates PRs, monitors required checks, notifies reviewer, detects human merge, and marks DONE |
| `app/agents/*` | Separate planner/coder/reviewer Codex CLI wrappers and structured schemas | Simplify | One implementation invocation per task (plus bounded repair invocations) and one fresh read-only review invocation |
| `app/test_runner.py` | Dev Container startup/cleanup and repository-specific deterministic validation | Keep | Run validation on the runner; return structured result and bounded diagnostics |
| Dashboard | Task list, statistics, repository CRUD, and service status | Keep and simplify display model | Show lifecycle, phase, runner, lease/heartbeat, repair count, PR, CI, and last error from durable records |
| Kubernetes manifests | Scheduler and dashboard deployments/configuration; PostgreSQL is external | Keep control plane in k3s | Scheduler stays in k3s; Mac runner connects outbound to existing PostgreSQL; install it with launchd |

## State, retries, Codex calls, and side effects

- `TaskStatus` currently has 12 values: `QUEUED`, `PLANNING`,
  `IMPLEMENTING`, `VALIDATING`, `REVIEWING`, `PUBLISHING`, `WAITING_CI`,
  `FINAL_REVIEW`, `READY`, `COMPLETED`, `BLOCKED`, and `FAILED`.
- Task rows retain three numeric counters (`implementation_attempts`,
  `validation_attempts`, `ci_attempts`); workflow metadata also retains
  final-review repair counts and other phase-specific retries. Configuration
  includes `MAX_ATTEMPTS`, `MAX_FINAL_ATTEMPTS`,
  `MAX_FINAL_REVIEW_ATTEMPTS`, and `CI_RETRY_ATTEMPTS`. These are not yet one
  shared repair budget.
- A normal run can invoke the planner, a coder and reviewer for each plan step,
  final integration/review nodes, and a fresh PR-head review after CI. The
  count grows with plan length and repair loops; there is no single normal-path
  invocation count.
- GitHub side effects are split between graph nodes and the scheduler. The
  workspace layer uses stable operation IDs and remote preconditions; the
  scheduler upserts status comments and reconciles existing PRs. These
  idempotency checks must survive the workflow simplification.
- PostgreSQL task rows and LangGraph checkpoints are separate records in the
  configured schema. Recovery currently depends on worker PID/lease metadata,
  task status, graph checkpoint history, branch state, and side-effect intents.

## Deployment and migration baseline

- The deployed architecture has a k3s scheduler and dashboard, the existing
  PostgreSQL service, GitHub, and one physical Mac runner. The scheduler
  initiates SSH; the Mac does not currently pull jobs from PostgreSQL.
- The current manifest sets `MAX_ACTIVE_TASKS`, `MAX_CODEX_PROCESSES`, and
  `MAX_BUILDS` to one. There is no dedicated runner Deployment in k3s.
- `.env.example` documents 32 environment-variable names. SSH, task DB,
  checkpoint DB, GitHub, validation, model, retry, and capacity settings are
  mixed together; there is no typed settings model.
- At audit time, two local untracked runner files and tracked edits were
  already present. They were preserved. The file-backed job spool is not a
  PostgreSQL queue. No production execution code has been removed.
- A read-only live task query on 2026-10-08 found no tasks in active worker
  phases; the database contained two `COMPLETED` tasks and one `BLOCKED` task
  (`spider-su/investory#109`, with no PR). This is the current migration
  boundary; the blocked task's history/checkpoint must be preserved.

## Baseline validation

The first system-Python run was an environment failure: its interpreter lacked
the repository dependencies. Running the suite with `.venv/bin/python`
completed successfully: **184 tests passed**. This is the pre-migration
baseline. The existing suite is primarily unit and mocked integration
coverage; it does not prove PostgreSQL row-locking, Mac daemon restart, or a
live end-to-end workflow.

## Migration decisions and gates

### Target contracts

Lifecycle state and observable phase are separate:

```text
QUEUED -> RUNNING -> WAITING_CI -> READY -> DONE
   |         |           |          |
   +------> BLOCKED <-----+----------+
               |
               +---- explicit human retry ----> QUEUED
```

CI repair returns `WAITING_CI` to `RUNNING`; an unmerged closed PR becomes
`BLOCKED` with a specific reason. `DONE` is terminal. The phases are
`preparing`, `implementing`, `validating`, `reviewing`, `repairing`, and
`publishing`; they do not independently control lifecycle transitions.

The minimum durable job kinds are `IMPLEMENT`, `REVIEW`, and `REPAIR`. A job
has immutable task/repository/base/branch/SHA/prompt/config/capability context,
status, timestamps, current runner and attempt IDs, lease expiry, result, and
structured failure. `job_attempts` records each claim independently. A
completion, heartbeat, or result update is accepted only from the current
attempt ID. Expired leases are marked uncertain and are not made claimable
until the previous process is proven stopped and its workspace fenced.

### Migration decisions and gates

1. Add PostgreSQL job/attempt/runner records and atomic claims without changing
   the active SSH dispatcher. Require attempt fencing and reject stale
   heartbeats/completions.
2. Add the pull runner and launchd configuration. Test SQLite-level behavior
   and run the PostgreSQL locking suite against an isolated PostgreSQL test
   database; do not use the production schema as a test fixture.
3. Add a transport flag. Keep the old dispatcher the default until direct
   prompt, issue, repair, restart, and recovery scenarios pass with one
   dispatcher enabled at a time.
4. Move execution to the runner and reconciliation to bounded scheduler
   passes. Preserve task records, checkpoints, branch commits, PR references,
   and side-effect intents for legacy tasks.
5. Change lifecycle states and retry accounting only after a data migration
   maps every existing status and preserves the three live task records.
6. Remove SSH transport, graph checkpoint code, history rewriting, and unused
   dependencies only after the pull path is operational and rollback has been
   exercised.

### Implemented in this migration increment

- Added PostgreSQL/SQLite `jobs`, `job_attempts`, and `runners` storage with
  immutable specifications, secret-key rejection, attempt-fenced leases,
  atomic capability/resource claims, stale-runner rejection, and same-task /
  same-branch exclusion.
- Added an opt-in scheduler pull transport for implementation and final-review
  jobs, restart-aware job reservations, task result reconciliation, fresh
  runner/capability checks, and runner-side quota/auth gating. The worker still
  calls the existing task CLI; this does not implement the target
  single-invocation Codex flow.
- Added focused SQLite tests for dispatch and reconciliation. The complete
  **204-test suite passed with no skips**, including the PostgreSQL atomic
  claim test against the isolated `.60` development schema. Scheduler/runner
  restart behavior and live end-to-end workflows remain unverified. SSH
  remains the deployed default; pull dispatch is opt-in and must not be enabled
  in production before acceptance is complete.

Human review and merging remain required. The scheduler does not merge PRs or
create branch-promotion PRs.

### Manual merge policy cutover

The scheduler no longer merges READY task PRs after an approving review and no
longer creates development-to-release promotion PRs. An approval alone leaves
the task READY. The scheduler marks it complete and closes the linked issue
only after observing the human merge and successful post-merge CI. The old
GitHub client helper methods remain unused pending the broader removal stage.

### Follow-up implementation update (2026-10-08)

- Added opt-in `WORKFLOW_MODE=simplified`. New tasks consolidate the planner's
  ordered plan into one implementation step and skip per-step reviews; they
  still receive deterministic validation and the final independent review.
  The planner and coder are still separate Codex invocations, and the legacy
  path remains the default. Saved checkpoints without a mode stay on the
  legacy route.
- The current repository suite passes **206 tests with one skipped PostgreSQL
  integration test**. The test was attempted against the documented `.60`
  development endpoint, but the password in the local profile was rejected.
  The active datasource in that profile points to Neon; no Neon database was
  modified. PostgreSQL locking is therefore unverified in this checkout.
- Live pull-runner, scheduler/runner restart, and full issue-to-PR workflows
  remain unverified. The deployed scheduler configuration continues to select
  SSH and `WORKFLOW_MODE=legacy` until those acceptance gates pass.
- Added an atomic, durable `MAX_REPAIRS` budget in task metadata. Graph repair
  invocations reserve from it across validation, review, final integration,
  and CI repair; failed starts without a candidate refund the reservation.
  SQLite tests cover concurrent reservations, restart persistence, exhaustion,
  and refund. The latest suite passes **210 tests with one skipped**; the
  PostgreSQL integration test remains unverified because the `.60` credentials
  could not authenticate.
- Migrated the task lifecycle to `QUEUED`, `RUNNING`, `WAITING_CI`, `READY`,
  `DONE`, and `BLOCKED`; prior phase-specific statuses are normalized at task
  store initialization and their last phase is retained in metadata. The
  dashboard now shows phase separately. The latest suite passes **214 tests
  with one skipped**. This migration has only been exercised with SQLite in
  this checkout; PostgreSQL and production task rows still need a controlled
  migration/acceptance run.
