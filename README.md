# Investory Orchestrator

Investory Orchestrator turns queued GitHub issues or direct prompts into
reviewed draft pull requests. Planner, coder, and reviewer roles run through
the local Codex CLI; GitHub operations use a GitHub App. It coordinates
deterministic validation, isolated Git checkouts, GitHub Actions, repair loops,
and a durable task queue backed by SQLite for local use or PostgreSQL for
deployment.

It does not merge pull requests automatically. Human review remains the final
approval step.

## Current status

The k3s/devMac flow has been exercised on Issue #20 through repository
validation and final independent review. That run stopped before PR publication
because the existing branch had no new changes to commit. A complete PR
lifecycle and unattended production use remain unverified.

[`ROADMAP.md`](ROADMAP.md) is the authoritative source for implementation,
verification, and production-readiness status.

The k3s scheduler, PostgreSQL, devMac SSH runner, dashboard, and deployment
setup are documented in [`docs/k3s-poc.md`](docs/k3s-poc.md).

## What it does

```text
GitHub issue or direct task prompt
→ persistent queued task
→ isolated Git checkout and branch
→ plan, implement, validate, review, repair
→ draft pull request
→ wait for GitHub Actions without holding a worker slot
→ repair CI failures and push an update
→ final independent review
→ READY or BLOCKED
```

The current executable workflow also includes several operational-hardening
features, such as isolated retries, whole-plan review, integration repair, and
final history rewriting. These features are described in
[`docs/architecture.md`](docs/architecture.md).

## Quick start

GitHub issues with the configured ready label are checked against
[`docs/issue-contract.md`](docs/issue-contract.md) before they are queued. An
invalid issue receives a comment explaining the missing information; its ready
label remains in place, and no workspace or Codex process is started.

Queue an issue or a direct task:

```bash
docker compose run --rm orchestrator python -m app --submit-issue <number>
docker compose run --rm orchestrator python -m app \
  --submit-task "Fix portfolio export" --body "Acceptance criteria..."
```

Start the persistent queue and inspect tasks:

```bash
docker compose run -d --name investory-orchestrator orchestrator \
  python -m app --run-queue
docker compose run --rm orchestrator python -m app --list-tasks
docker compose run --rm orchestrator python -m app --status <task-id>
docker compose run --rm orchestrator python -m app \
  --reconcile-merged-pr '<task-id>'
```

By default, the SQLite task database and LangGraph checkpoints live under
`./data`; set `DATABASE_URL` to use PostgreSQL for both.
Each task receives `workspaces/task-<id>` or `workspaces/issue-<number>` and a
separate branch. A shared bare repository cache under `workspaces/.repositories`
backs independent local clones so Dev Containers receive the checkout's Git
metadata. `--run-queue --once` runs one queue pass for
supervised operation. The default queue keeps polling until stopped.

Set `BASE_BRANCH` to the target repository's base branch. Resource defaults are
`MAX_ACTIVE_TASKS=3`, `MAX_CODEX_PROCESSES=2`, and `MAX_BUILDS=1`; the scheduler
uses the tightest limit. A worker exits after publishing its PR, so waiting for
CI does not occupy a worker slot. CI and final-review repairs are bounded by
`CI_RETRY_ATTEMPTS` (default 3). The existing implementation and validation
repair loops use `MAX_ATTEMPTS` and `MAX_FINAL_ATTEMPTS`. Mac-side final-review
attempts and timeout use `MAX_FINAL_REVIEW_ATTEMPTS` and
`MAC_REVIEW_TIMEOUT_SECONDS`.

With `MAC_SSH_TARGET` configured, whole-plan final review runs on the Mac
runner against the exact open PR head. The scheduler polls READY tasks after
human merge and records completion only after CI on the merge commit succeeds;
it then closes the issue linked by the PR. For a merge made before scheduler
tracking, `--reconcile-merged-pr '<task-id>'` verifies the merge, target branch,
linked issue, and post-merge CI before recording the human merge.

The planner, coder, and reviewer use the authenticated Codex CLI, not the
OpenAI API. Set `HOST_CODEX_DIR` to the host's authenticated Codex directory.
Set explicit model identities with `CODER_MODEL`, `PLANNER_MODEL`, and
`REVIEWER_MODEL`. To qualify the final review as independent, the coder and
reviewer model IDs must both be known and different. The task cannot become
READY when those identities match or are missing.

## Current limitations

- The scheduler polls each enabled repository at its configured interval for
  open issues labeled `ready_to_develop`. The issue body is the task prompt;
  after the task is durably queued, the label is removed and a stable status
  comment is posted. The dashboard tracks progress, and a GitHub mention is
  posted when the PR is ready for human review.
- Live GitHub, Dev Container, coder, and GitHub Actions end-to-end scenarios
  have not yet been run for the new queue lifecycle.
- Interrupted coder work with an uncommitted diff is preserved and blocked for
  inspection instead of being reset automatically.
- Existing graph recovery still needs broader crash-boundary testing around
  local commits and final history rewriting.
- Human approval and merge remain outside the orchestrator; merged PRs are
  tracked afterward and successful tasks move to `COMPLETED`.
- Codex execution depends on available authentication and usage quota.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — workflow graph, state,
  retries, checkpoints, finalization, and module responsibilities
- [`docs/issue-contract.md`](docs/issue-contract.md) — ready-to-develop issue
  standard and future automatic preflight
- [`docs/operations.md`](docs/operations.md) — configuration, credentials,
  commands, validation, inspection, and recovery
- [`ROADMAP.md`](ROADMAP.md) — authoritative status and unfinished work
