# Operations

This document covers configuration, credentials, execution, inspection, and
recovery. Workflow design is documented in [`architecture.md`](architecture.md).

## Preconditions

Before starting a run:

1. Add the `ready_to_develop` label. The scheduler formats non-empty issue
   descriptions that miss the contract headings and preserves the original
   text before creating a task or workspace.
2. Verify GitHub App credentials and repository configuration.
3. Verify the coding backend is authenticated and has available quota.
4. Confirm the target repository supplies its Dev Container and validation
   entry point, and that the entry point runs the repository formatter check
   and unit tests, failing if either is skipped or red.

Only an empty issue or an unsafe, unresolved product decision should stop
preflight. A formatting pass does not consume a Codex run.

## Configuration

Common environment settings:

```env
GITHUB_APP_ID=...
GITHUB_INSTALLATION_ID=...
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-key
GITHUB_REPOSITORY=spider-su/investory

WORKSPACES_DIR=/app/workspaces
TASK_DB=/app/data/tasks.db
BASE_BRANCH=main
MAX_ATTEMPTS=2
MAX_REPAIRS=3
WORKFLOW_MODE=simplified
MAX_FINAL_ATTEMPTS=1
CI_RETRY_ATTEMPTS=3
MAX_ACTIVE_TASKS=3
MAX_CODEX_PROCESSES=2
MAX_BUILDS=1
CODEX_QUOTA_THROTTLE_REMAINING_PERCENT=10
CODEX_QUOTA_PAUSE_REMAINING_PERCENT=5
QUEUE_POLL_SECONDS=30
READY_ISSUE_LABEL=ready_to_develop
PUBLISH_PLAN_COMMENT=true
PUBLISH_REVIEW_COMMENT=true
TARGET_ADAPTER=devcontainer_script
AGENT_DEVCONTAINER_SCRIPT=scripts/agent-devcontainer.sh
PLANNER_MODEL=
CODER_MODEL=
REVIEWER_MODEL=
```

`WORKFLOW_MODE` accepts `legacy` or `simplified`. New tasks default to
`simplified`, which consolidates the full plan and caps coding at two rounds.
Existing checkpoints retain their saved mode. Legacy mode is available for
compatibility only.

`MAX_REPAIRS` is the task-wide limit for actual code repair invocations across
local validation/review and CI/final-review repair. The limit and usage are
persisted in task metadata; infrastructure failures that produce no candidate
refund the reservation. CI polling and runner health retries do not consume it.

Each red CI result is saved with all failed check-run summaries and GitHub
annotations. Annotations retain their repository path, line/column range,
diagnostic, and source link; the coder receives every failed check and location
on a repair run. CI can trigger at most three repair rounds by default, further
limited by `MAX_REPAIRS`.

Task status reports one of six lifecycle states. The dashboard shows the
separate execution phase (preparing, implementing, validating, reviewing,
repairing, or publishing) so the lifecycle does not need a separate status for
each workflow node.

The scheduler polls enabled repositories at each repository's configured
`poll_interval_seconds` (minimum 30 seconds). It formats malformed, non-empty
issue descriptions in place and preserves the original text. The normalized
issue is persisted, receives a stable status comment, and has its label
removed. The dashboard shows
task progress and Mac runner/queue health. Once CI and independent final review
pass, the draft PR is marked ready for review and the configured GitHub login
is mentioned. You review and merge the PR. After successful post-merge CI, the
scheduler records completion and closes the linked issue.

Planner, coder, and reviewer all run as separate local Codex CLI invocations.
They authenticate through the mounted Codex home directory; they do not use
`OPENAI_API_KEY` or an OpenAI API project. Set role-specific model IDs only to
models available to that Codex CLI account; replace the blank model settings in
`.env` with the exact IDs you intend to use. The reviewer runs with a read-only
sandbox and receives a fresh CLI invocation.

## Task queue

Queue a GitHub issue or a direct task prompt:

```bash
python -m app --submit-issue 123
python -m app --submit-task "Fix portfolio export" --body "Expected behavior..."
```

Run the persistent queue, inspect status, or run one pass:

```bash
python -m app --run-queue
python -m app --run-queue --once
python -m app --list-tasks
python -m app --status <task-id>
python -m app --reconcile-merged-pr '<task-id>'
```

`TASK_DB` must be on persistent storage. Compose mounts `./data` for task
records and LangGraph checkpoints. The scheduler claims work atomically in
SQLite and applies the minimum of the active-task, Codex-process, and build
limits. A task in `WAITING_CI` has no active worker. The scheduler polls GitHub
Actions and commit statuses, starts bounded CI/final-review repair, and marks a
task `READY` only after all required checks pass. `--once` is intended for
supervised runs; omit it for continuous polling.

In the k3s/Mac deployment, scheduler health probes the Mac over SSH every
`RUNNER_HEALTH_CHECK_SECONDS` (default 60). The probe checks runner tools and
authentication, writable workspaces, a clean checkout, and that the runner
checkout matches the scheduler image revision. It also reads Codex's primary
and secondary account rate-limit windows through the local Codex app-server,
without making a model call. The dashboard's **Runner and queue** card shows
the quota state. At 10% remaining, new work is limited to one active task; at
5% or less, new dispatch pauses. If the quota snapshot cannot be read, dispatch
pauses until a fresh snapshot is available.

Quota or authentication failures pause new Codex dispatch globally instead of
letting each issue fail in turn. A prior quota-triggered pause clears when a
fresh quota snapshot confirms usage is above the pause threshold. Authentication
only pauses still require the login to be repaired, then cleared with
`python -m app --resume-queue`. Configure `CODEX_QUOTA_THROTTLE_REMAINING_PERCENT`
and `CODEX_QUOTA_PAUSE_REMAINING_PERCENT` to tune the 10% throttle and 5% pause
defaults; the pause threshold must be lower than the throttle threshold.

The scheduler records worker lease owner, PID, start time, and heartbeats in
task metadata. SSH keepalives and the Mac-side task lock allow the scheduler to
distinguish a dropped SSH session from a still-running worker.

In the split k3s/Mac deployment, final review is sent to the Mac runner over
SSH. The runner checks that its workspace is clean and that its branch and
HEAD match the open PR before it starts the read-only review. After a human
merges a READY PR, the scheduler waits for successful checks on the merge
commit, records the merge evidence, and closes the linked issue. To reconcile a
merge made before the scheduler observed it, run
`python -m app --reconcile-merged-pr '<task-id>'`; this requires a linked issue,
the configured base branch, a recorded GitHub merge actor, and successful
post-merge CI.

Final review qualification requires known, different coder and reviewer model
IDs. Set `CODER_MODEL` and `REVIEWER_MODEL` to different, explicit model IDs
available to the local Codex CLI. If either ID is blank or they match, the
final review is recorded but the task remains blocked instead of claiming an
independent approval.

## Reviewer independence checks

Before describing an automated review as independent, record these values in
the run metadata:

- coder backend, provider, and model identity
- reviewer backend, provider, and model identity
- confirmation that the reviewer invocation used fresh context
- confirmation that the reviewer had read-only access
- deterministic validation command and result

The reviewer must use a different model identity from the coder. A different
provider is preferred but not required. The reviewer must not receive coder
chain-of-thought, hidden reasoning, or the coder's session history.

When the model identities are equal or unavailable, report the result as:

```text
secondary automated review
```

Do not report it as an independent review in logs, issue comments, or pull
request summaries.

The implementation persists coder/reviewer identities, fresh-context and
read-only evidence, and labels the result `independent` only when both model
identities are available and differ. Otherwise it labels the result
`secondary automated review`.

## Credentials

Set `HOST_CODEX_DIR` to the authenticated host Codex directory (usually
`/Users/<you>/.codex` on macOS). Compose mounts it read-only at
`/root/.codex-source` and copies it into a writable container-local
`/root/.codex` runtime directory at startup. All three agent roles use this
login, so no OpenAI API key is needed:

```yaml
services:
  orchestrator:
    volumes:
      - ${HOST_CODEX_DIR}:/root/.codex-source:ro
```

Store the GitHub App PEM at `secrets/github-app.pem` on the host; Compose
mounts it at `GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-key`. GitHub
credentials and `OPENAI_API_KEY` are removed from every Codex child process.
Do not commit GitHub private keys, Codex credentials, or generated installation
tokens. Git push uses a short-lived GitHub App installation token.

## Run and resume

Run a new issue:

```bash
docker compose run --rm orchestrator \
  python -m app --issue <number>
```

Resume a blocked issue:

```bash
docker compose run --rm orchestrator \
  python -m app --issue <number> --resume
```

`python -m app.graph` remains a compatibility entry point while the workflow
implementation is split into smaller packages.

The thread ID is stable for the issue number. Resume loads the saved LangGraph
checkpoint and continues from the persisted blocked stage.

When planning blocks on unresolved product questions, update the GitHub issue
with the required decisions and then run `--resume`. The orchestrator reloads
the issue, refreshes repository context, and creates a new implementation plan
before coding starts.

For all other blocked stages, do not use `--resume` to silently change product
requirements. Update the issue explicitly only when the saved plan remains
valid, or restart the workflow after explicit recovery.

### Remote side-effect recovery

Before pushing a branch or creating/updating a draft pull request, the graph
persists a deterministic operation ID and the expected remote state.

On resume:

- a branch already pointing at the intended final commit is treated as an
  applied push;
- an unchanged remote branch is retried with `--force-with-lease`;
- a branch changed by another actor blocks instead of being overwritten;
- an existing open pull request for the issue branch is updated rather than
  duplicated.

`--resume` also accepts an interrupted checkpoint whose prepared remote
operation has not yet returned a normal blocked state. Push recovery reuses the
saved remote precondition; it never refreshes that precondition before retry.

## Blocked workflows

A workflow blocks when an environment, coder, reviewer, validation, push, or PR
stage cannot continue safely. The workspace and checkpoint remain available for
inspection.

Resume is stage-aware:

- coder or validation failure resumes implementation of the current step
- reviewer failure resumes the review or repair path
- push failure resumes at push
- PR failure resumes at PR reconciliation or creation

Before resuming, fix the external condition when the failure is infrastructure
related, such as credentials, quota, provider availability, or Dev Container
startup.

## Resume reconciliation procedure

Do not invoke `--resume` blindly after a process crash near a side effect. The
checkpoint may contain only the prepared intent even when the operation already
completed. Until write-ahead operation records and automatic reconciliation are
implemented for every node, these boundaries require manual inspection.

For a pending or uncertain operation:

1. Stop automated retries and preserve the checkpoint, workspace, and run
   artifacts.
2. Record the saved operation intent, expected before-state, expected
   after-state, and operation identifier.
3. Inspect the actual workspace, local Git refs, remote branch, comments, and PR
   state as applicable.
4. Classify the operation as **not applied**, **applied**, or **ambiguous**.
5. Retry only a not-applied operation. Adopt an applied result into workflow
   state without repeating it. Block and repair explicitly when the result is
   ambiguous or divergent.

Useful local and remote checks include:

```bash
git status --short --untracked-files=all
git rev-parse HEAD
git rev-parse refs/heads/agent/issue-<number>
git log --format='%H%n%B%n---' --all -20
git ls-remote origin refs/heads/agent/issue-<number>
```

Inspect GitHub for an existing open PR by the exact head branch before creating
a PR. Inspect issue comments for their stable marker before publishing another
comment.

### Boundary-specific recovery

- **Coder crashed with changed files:** archive the complete tracked and
  untracked diff as an uncertain attempt, reset to the saved step baseline, and
  do not increment the attempt a second time.
- **Validation completed but state was not saved:** adopt a complete result only
  when its candidate, command, and environment fingerprints match. Otherwise
  rerun deterministic validation.
- **Commit succeeded but its SHA was not saved:** locate a commit with the saved
  operation trailer, expected parent, and expected tree; adopt that SHA instead
  of creating another commit.
- **Final history rewrite may have completed:** compare the issue branch with the
  saved checkpoint tip and expected final commit. Adopt the final commit when it
  matches; complete the compare-and-set update only when the branch still points
  to the checkpoint tip; block on any other ref.
- **Push may have succeeded:** compare the remote ref with the intended target.
  Target means success, the expected old SHA permits retry, and any other SHA is
  a conflict.
- **PR state was not saved:** query by repository, base, and head. Adopt and
  update the existing PR; create only when no matching PR exists.

The full node contract and operation-record schema are defined in
[`architecture.md`](architecture.md#resume-safety-contract).

## Inspect a workspace

```bash
cd workspaces/issue-<number>
git status
git log --oneline --decorate -10
```

Failed-attempt diagnostics are stored under:

```text
runs/issue-<number>/<step-id>/
```

Do not manually edit a blocked workspace unless the recovery procedure requires
it and the resulting state is reconciled with the workflow checkpoint.

## Operational checks

Run the same project validation used by CI (compile every Python module and
run all unit tests):

```bash
docker compose run --rm --entrypoint sh orchestrator scripts/ci-validate.sh
```

Both `app/` and `tests/` are bind-mounted by Compose, so local source and test
edits are visible to this command without rebuilding the orchestrator image.

Verify Codex inside the orchestrator container:

```bash
docker compose run --rm orchestrator \
  codex exec "Reply only with: container Codex works"
```

## Target repository requirements

The target repository should provide:

- `AGENTS.md` with repository-specific agent rules
- `.devcontainer/devcontainer.json`
- a Dev Container command script at
  `scripts/agent-devcontainer.sh`, or a path configured through
  `AGENT_DEVCONTAINER_SCRIPT`
- support in that script for `up <issue-number>`, `validate <issue-number>`,
  and `down <issue-number>` actions
- accept `--result-file <absolute-path>` for every action and atomically write
  a JSON result containing `status`, `exit_code`, and optional `message`
- validation that exits non-zero on failure
- a default branch compatible with the configured PR base

`TARGET_ADAPTER` selects the target integration. The only supported value is
currently `devcontainer_script`.

The script result `status` must be exactly one of:

- `success`
- `project_validation_failure`
- `environment_failure`

The script's reported `exit_code` must match its process exit code. The runner
classifies process spawn failures, timeouts, missing or malformed result files,
and protocol violations as `environment_failure`. It never infers validation
meaning from the process exit code; only the structured result controls whether
the workflow retries the coder.

The current Investory target uses a Dev Container and Maven-based validation.
When an issue explicitly limits its scope to documentation and says not to run
application tests, the runner skips Dev Container startup and Maven. It instead
runs `git diff --check`, records `git status`, and rejects changed or untracked
files outside documentation extensions. The coder and reviewer still run, and
their reports remain subject to the issue's acceptance criteria.

## Pull-request behavior

The orchestrator pushes `agent/issue-<number>`, reuses an existing open PR for
that branch when present, and otherwise creates a draft PR. Completion is
recorded only after the PR operation succeeds.

Task PRs are prepared for human review. The scheduler never merges them. After
you merge a PR, it verifies the merge target, issue linkage, and post-merge CI
before recording task completion and closing the linked issue.

## Retention and cleanup

Blocked workspaces are intentionally preserved for inspection and resume.
Explicit workspace, checkpoint, run-artifact, and abandoned-issue retention
policies are still planned. Do not delete a blocked workspace until its
checkpoint is no longer needed.

## PostgreSQL pull-runner migration (opt-in)

`RUNNER_TRANSPORT=postgres_pull` opts the scheduler into PostgreSQL job
dispatch. The default remains `ssh`. In pull mode, the scheduler stores an
immutable implementation or review job before returning to its polling loop;
the Mac daemon claims jobs and reports attempt-fenced results. The scheduler
maps job results back to task state and waits for final review asynchronously.
An online runner with `codex`, `git`, `build`, and `review` capabilities plus a
fresh Codex quota report is required before dispatch.

Jobs use renewable leases; an expired lease becomes `uncertain` and is never
requeued automatically. An operator must prove that the previous process
stopped before invoking the guarded requeue operation. Claims reject stale
runners and serialize writes to the same repository branch.

Pull transport remains opt-in until it passes the direct-prompt, GitHub issue,
CI repair, scheduler restart, runner restart, and live deployment acceptance
scenarios below. Keep one dispatcher active at a time. Do not switch the
deployed scheduler to pull mode before the Mac LaunchAgent has a matching code
checkout, database access, and verified quota health.

To prepare a devMac LaunchAgent after that migration is approved:

1. Check out the same reviewed code revision as the scheduler and install its
   Python dependencies. The runner uses the same PostgreSQL schema as the
   scheduler and needs network access to that database.
2. Create `~/.config/investory-orchestrator/runner.env` with `DATABASE_URL`,
   `RUNNER_WORKSPACES_DIR`, `RUNNER_RUNS_DIR`, `RUNNER_LOG_DIR`, and
   `RUNNER_RESULT_DIR`. Keep credentials in this local file, set mode `0600`,
   and never put them in a job specification.
3. Copy
   `scripts/com.spider-su.investory-orchestrator.runner.plist.example` to
   `~/Library/LaunchAgents/com.spider-su.investory-orchestrator.runner.plist`,
   replace `YOUR_USER` and the repository path, then validate with
   `plutil -lint`.
4. After pull dispatch is enabled and approved, load it with
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.spider-su.investory-orchestrator.runner.plist`.
   Inspect logs and runner heartbeat before accepting work. Unload it with
   `launchctl bootout gui/$(id -u)/com.spider-su.investory-orchestrator.runner`.

The pull worker still invokes the existing task CLI and the existing task-state
model remains in place. Pull transport is available for acceptance testing but
is not the deployed default.

See [`postgres-runner-acceptance.md`](postgres-runner-acceptance.md) for the
verified transaction and one-shot runner evidence, plus the outstanding
deployment and end-to-end acceptance steps.

### PostgreSQL integration tests on the dev database

The Investory development PostgreSQL server at `192.168.1.60` is reserved for
development and isolated integration tests. It is not production and must never
be used for production data or production migrations. Connection settings come
from the development-only JDBC stanza in
`/Users/alex/projects/investory/app/src/main/resources/application-local.yml`;
do not copy credentials into this repository or print them in logs. The active
`local` profile may point to another database, so select the `.60` stanza
explicitly and verify the JDBC target before creating objects.

The pull-runner PostgreSQL integration tests create a unique schema for each
test under an operator-supplied prefix such as
`orchestrator_test_20261009_a1b2c3d4`. They drop only that unique schema after
the test. Never point these tests at production. Build a local
`TEST_POSTGRES_URL` like
`postgresql://USER:PASSWORD@192.168.1.60:5432/inventory?client_encoding=UTF8`
from the `.60` JDBC settings in the local shell, set
`client_encoding=UTF8` in the URL (the development database uses `SQL_ASCII`),
and choose a fresh test prefix:

```bash
export TEST_POSTGRES_SCHEMA="orchestrator_test_$(date -u +%Y%m%d)_$(python -c 'import secrets; print(secrets.token_hex(4))')"
python -m unittest tests.test_runner_job_store -v
```

Keep the `TEST_POSTGRES_URL` value local and out of `.env.example`, commits,
command transcripts, and task specifications. Pull-request CI runs these
transaction tests against an ephemeral PostgreSQL service; the real development
database remains an opt-in local acceptance target.
