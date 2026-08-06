# Operations

This document covers configuration, credentials, execution, inspection, and
recovery. Workflow design is documented in [`architecture.md`](architecture.md).

## Preconditions

Before starting a run:

1. Manually verify the issue against [`issue-contract.md`](issue-contract.md).
2. Verify GitHub App credentials and repository configuration.
3. Verify the coding backend is authenticated and has available quota.
4. Confirm the target repository supplies its Dev Container and validation
   entry point.

The current CLI does not reject an invalid issue before workspace creation or
planner invocation.

## Configuration

Common environment settings:

```env
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY=...
GITHUB_INSTALLATION_ID=...
GITHUB_REPOSITORY=spider-su/investory

WORKSPACES_DIR=/home/alex/investory-orchestrator/workspaces
MAX_ATTEMPTS=3
MAX_FINAL_ATTEMPTS=3
PUBLISH_PLAN_COMMENT=true
PUBLISH_REVIEW_COMMENT=true
```

Planner and reviewer settings may target OpenAI, Azure OpenAI, or an
OpenAI-compatible gateway such as Open WebUI. The current coding backend uses
Codex CLI.

## Credentials

When Codex CLI is used, mount the authenticated host Codex directory into the
orchestrator container user's home. For a root container user:

```yaml
services:
  orchestrator:
    volumes:
      - ${HOME}/.codex:/root/.codex
```

Do not commit API keys, GitHub private keys, Codex credentials, or generated
installation tokens. Git push uses a short-lived GitHub App installation token.

## Run and resume

Run a new issue:

```bash
docker compose run --rm orchestrator \
  python -m app.graph --issue <number>
```

Resume a blocked issue:

```bash
docker compose run --rm orchestrator \
  python -m app.graph --issue <number> --resume
```

The thread ID is stable for the issue number. Resume loads the saved LangGraph
checkpoint and continues from the persisted blocked stage.

Do not use `--resume` to silently change product requirements. Update the issue
explicitly and ensure the saved plan remains valid.

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

Compile the orchestrator modules:

```bash
docker compose run --rm orchestrator \
  python -m py_compile \
  app/state.py \
  app/github_client.py \
  app/workspace.py \
  app/repository_context.py \
  app/test_runner.py \
  app/agents/planner.py \
  app/agents/coder.py \
  app/agents/reviewer.py \
  app/graph.py
```

Verify Codex inside the orchestrator container:

```bash
docker compose run --rm orchestrator \
  codex exec "Reply only with: container Codex works"
```

## Target repository requirements

The target repository should provide:

- `AGENTS.md` with repository-specific agent rules
- `.devcontainer/devcontainer.json`
- a deterministic validation entry point
- validation that exits non-zero on failure
- a default branch compatible with the configured PR base

The current Investory target uses a Dev Container and Maven-based validation.

## Pull-request behavior

The orchestrator pushes `agent/issue-<number>`, reuses an existing open PR for
that branch when present, and otherwise creates a draft PR. Completion is
recorded only after the PR operation succeeds.

The orchestrator does not merge automatically. Human review is always required.

## Retention and cleanup

Blocked workspaces are intentionally preserved for inspection and resume.
Explicit workspace, checkpoint, run-artifact, and abandoned-issue retention
policies are still planned. Do not delete a blocked workspace until its
checkpoint is no longer needed.
