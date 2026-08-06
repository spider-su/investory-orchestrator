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

The current implementation does not yet enforce or persist this contract.
Until enforcement exists, operators must verify model separation manually and
treat automated review as secondary when the evidence is incomplete.

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

When planning blocks on unresolved product questions, update the GitHub issue
with the required decisions and then run `--resume`. The orchestrator reloads
the issue, refreshes repository context, and creates a new implementation plan
before coding starts.

For all other blocked stages, do not use `--resume` to silently change product
requirements. Update the issue explicitly only when the saved plan remains
valid, or restart the workflow after explicit recovery.

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
