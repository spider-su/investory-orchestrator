# Architecture

This document describes the executable workflow and its design invariants.
Capability status is tracked only in [`../ROADMAP.md`](../ROADMAP.md).

## Workflow

```text
operator-approved GitHub issue
→ issue workspace and branch
→ bounded repository context
→ structured implementation plan
→ Dev Container startup
→ implement one plan step
→ deterministic validation
→ step review
→ local checkpoint commit
→ repeat remaining steps
→ final whole-plan validation
→ final whole-plan review
├── changes required → isolated integration repair
└── approved
    → rewrite checkpoint history
    → final logical commit
    → push branch
    → create or update draft PR
```

The workflow is supervised. The operator currently owns issue-readiness
preflight and final pull-request approval.

## Delivery stages

The architecture contains capabilities from two delivery stages:

- **Supervised MVP:** repository-aware planning, ordered implementation steps,
  deterministic validation, automated review, bounded retries, local
  checkpoints, resume, branch push, and draft-PR creation or update.
- **Operational hardening:** automatic issue-contract validation,
  failed-attempt artifacts, clean retry isolation, provisional checkpoint
  approval, whole-plan review, cross-step repair, history rewriting,
  independent CI, and unattended queue execution.

Hardening may be implemented in the current graph even when it is not part of
the MVP completion gate.

## Planning phase

1. Load the issue title and body from GitHub.
2. Prepare or reuse `workspaces/issue-<number>`.
3. Check out `agent/issue-<number>`.
4. Collect bounded repository context.
5. Ask the planner for a structured implementation plan.
6. Publish the plan to the issue when enabled.
7. Block when unresolved product questions remain.

Repository facts such as package roots, build tools, test conventions, and
entry points should be resolved from repository context rather than delegated
to the issue author.

## Implementation phase

For each normalized plan step:

1. Mark the step in progress.
2. Run the coder with the issue, current step, attempt metadata, previous
   validation output, reviewer findings, and relevant Git diff.
3. Run deterministic repository validation inside the Dev Container.
4. Route validation failures back to the coder while attempts remain.
5. Run the reviewer after validation succeeds.
6. Route blocking review findings back to the coder while attempts remain.
7. Create a local checkpoint commit after approval.

Later steps must not be implemented early. Step progression happens only after
approval of the current step.

## Retry semantics

`MAX_ATTEMPTS` is a per-step limit shared by validation and review repair loops.
An attempt is consumed when the coder produces a candidate:

```text
coder candidate
→ deterministic validation
→ automated review when validation succeeds
```

Infrastructure failures block the workflow without consuming an implementation
attempt. Examples include unavailable authentication, provider outage, Dev
Container startup failure, reviewer failure, or a coder timeout before a
candidate is produced.

## Review independence

Automated review has two separate layers:

1. **Deterministic validation** runs repository-owned commands and produces an
   objective pass or failure result.
2. **LLM review** evaluates the validated diff against the issue, plan, scope,
   acceptance criteria, repository rules, and validation output.

An LLM review may be called **independent** only when all of these conditions
hold:

- The reviewer runs as a new invocation with fresh context. It does not reuse
  the coder session, conversation memory, or hidden state.
- The reviewer has read-only access. It cannot modify the workspace, commit,
  push, or repair the implementation it is judging.
- The reviewer receives the issue contract, relevant plan scope, repository
  instructions, current diff, and deterministic validation result.
- The reviewer does not receive coder chain-of-thought, hidden reasoning,
  internal prompt transcript, or an instruction to defend the coder's design.
- The workflow records the coder and reviewer backend, provider, and model
  identity as review evidence.
- The reviewer model identity differs from the coder model identity.
- Deterministic validation runs outside the reviewer and must succeed before
  the LLM review can approve the change.

A different provider is preferred because it reduces correlated model and
infrastructure failures, but it is not mandatory. A different model identity
within the same provider is sufficient for the minimum independence contract
when the invocation and context are also isolated.

When the coder and reviewer use the same model identity, or when either model
identity cannot be established, the result must be described as a **secondary
review**, not an independent review.

The step reviewer and whole-plan reviewer may use the same reviewer backend and
model because independence is measured against the coder. Each review must
still use a fresh invocation and the minimum context required for its scope:

- step review receives the current step and its diff
- whole-plan review receives the complete plan and the full issue-baseline diff

Human pull-request review remains the final approval and is independent of both
automated layers.

### Clean retry isolation

Each step records its baseline as the approved `HEAD` at step start. A failed
attempt is diagnostic input, not an editable base for the next attempt.

```text
step baseline
→ coder attempt
→ validation
→ review
├── approved
│   → checkpoint commit
└── failed
    → archive patch and diagnostics
    → reset to step baseline
    → next attempt
```

Before reset, preserve:

- complete patch, including untracked files
- coder summary
- validation command, exit code, and output
- reviewer findings when review ran
- failure stage and attempt number

Suggested artifact layout:

```text
runs/
└── issue-<number>/
    └── <step-id>/
        ├── attempt-1.patch
        ├── attempt-1-summary.txt
        ├── attempt-1-validation.txt
        └── attempt-1-review.json
```

After preservation, reset should be equivalent to:

```bash
git reset --hard <step-baseline-sha>
git clean -fd
```

The next attempt receives the archived failure information as read-only
context. It remains constrained to the same issue, step, acceptance criteria,
scope, and change permissions.

## Checkpoints and final history

Step approval is provisional. A step reviewer proves the current step, not the
integrated architecture after all later steps exist.

Approved steps therefore create local checkpoint commits that:

- provide resume and recovery boundaries
- isolate subsequent step work
- remain local until final approval
- may be rewritten or removed
- are not final architectural decisions

After the final checkpoint, the orchestrator runs whole-plan validation and
reviews the complete diff from the issue baseline. The review covers issue-level
acceptance criteria, cross-step interactions, public API consistency,
migrations, configuration, compatibility, test coverage, duplication, and
scope growth.

When final validation or whole-plan review fails, an integration repair may
change code introduced by any checkpointed step while remaining inside the
original issue scope.

```text
checkpoint tip
→ integration repair
→ final validation
→ whole-plan review
├── failed
│   → archive diagnostics
│   → reset to checkpoint tip
│   → retry
└── approved
```

`MAX_FINAL_ATTEMPTS` bounds these repair attempts and defaults to
`MAX_ATTEMPTS`.

After approval, checkpoint history is replaced with one logical commit. The
orchestrator creates the commit object first, then compare-and-set updates the
local branch ref from the expected checkpoint tip:

```bash
git add -- <approved-paths>
git commit-tree <final-tree> -p <issue-baseline-sha>
git update-ref <branch-ref> <final-commit> <checkpoint-tip>
```

Only the final logical commit is pushed. Checkpoint SHAs remain in workflow
state for diagnostics but are no longer reachable from the issue branch.

## Completion and pull requests

After all steps are checkpointed:

1. Run final whole-plan validation.
2. Run final whole-plan review.
3. Perform bounded integration repair when required.
4. Replace checkpoint history with the final logical commit.
5. Push `agent/issue-<number>`.
6. Reuse an existing open PR for that branch, or create a draft PR.
7. Mark the workflow completed only after the PR operation succeeds.
8. Stop the Dev Container environment.

The orchestrator never merges automatically.

## Blocked state and resume

Environment, coder, reviewer, push, and PR failures block the workflow when it
cannot continue safely. Persisted state includes:

- issue, branch, workspace, and issue baseline
- implementation plan and normalized steps
- current and completed steps
- retry counters
- environment, validation, and review results
- checkpoint and final commit metadata
- failed stage and blocking reason
- pull-request metadata

Resume is stage-aware. A push failure resumes at push; a coder failure resumes
at implementation. Side-effecting stages must be idempotent or reconcile their
external state before repeating.

### Resume safety contract

Stage-aware routing is not sufficient when a node can mutate the workspace, Git
history, GitHub, or another external system. Every side-effecting node must
document and enforce four properties:

1. **Precondition** — the local and external state that must be true before the
   operation starts.
2. **Persisted intent** — the operation identifier, expected inputs, and
   expected before-and-after state written to the LangGraph checkpoint before
   the side effect begins.
3. **Idempotency mechanism** — the stable key or observable invariant that makes
   a repeated invocation safe.
4. **Reconciliation** — how resume determines whether an operation was not
   applied, was applied successfully, or completed ambiguously.

The graph must persist a write-ahead operation record before invoking the side
effect:

```text
pending_operation:
  operation_id: <stable deterministic key>
  node: <graph node>
  scope: <issue, step, attempt, or finalization scope>
  generation: <monotonic operation generation>
  status: prepared | applied | confirmed
  input_fingerprint: <tree, command, or request fingerprint>
  expected_before: <local or remote state>
  expected_after: <local or remote state>
  external_reference: <commit SHA, remote SHA, PR number, or artifact path>
```

`operation_id` remains stable across resume. A new operation identifier may be
created only after the previous operation is confirmed or explicitly abandoned.
The durable checkpoint may still show `prepared` after the side effect completed;
therefore the checkpoint is evidence of intent, not proof of reality.

On resume, inspect the real workspace and external system before running the
node again:

- **not applied** — observed state still matches `expected_before`; retry the
  same operation identifier
- **applied** — observed state matches `expected_after`; adopt the result and
  persist `confirmed` without repeating the side effect
- **ambiguous or divergent** — observed state matches neither boundary; block
  the workflow and require explicit recovery

Blindly replaying a side-effecting node is forbidden.

### Side-effecting node contracts

#### Workspace and branch preparation

- **Precondition:** the configured workspace path is absent, or it is already a
  clone of the configured repository with the expected issue branch and issue
  baseline.
- **Persisted intent:** repository identity, workspace path, branch name, source
  baseline SHA, and expected remote URL.
- **Idempotency:** workspace path and `agent/issue-<number>` are stable keys.
- **Reconciliation:** inspect the repository identity, branch, `HEAD`, origin,
  and worktree cleanliness. Reuse only an exact match; block rather than delete
  or overwrite a divergent workspace.

#### Coder attempt

- **Precondition:** `HEAD` equals the saved step baseline, the worktree is clean,
  and the attempt number has been reserved once for the operation identifier.
- **Persisted intent:** issue, step, attempt number, step-baseline SHA, starting
  tree fingerprint, scope constraints, and artifact destination.
- **Idempotency:** `(issue, step, attempt)` is unique. Attempt counters advance
  when the intent is prepared, not each time the node process starts.
- **Reconciliation:** when a coder crashes with a dirty tree, archive the full
  tracked and untracked diff as an uncertain candidate, reset to the saved
  baseline, and record the attempt as consumed exactly once. The next coder
  invocation uses the next attempt number. When the tree is clean and no
  candidate artifact exists, the same operation may be retried without
  incrementing the attempt. Never continue editing an ambiguous dirty
  workspace.

#### Deterministic validation

- **Precondition:** the candidate tree fingerprint matches the fingerprint saved
  in validation intent.
- **Persisted intent:** candidate tree fingerprint, validation command, Dev
  Container identity, result-artifact path, and validation operation identifier.
- **Idempotency:** the validation key is derived from candidate tree, command,
  and environment identity. Validation must not modify source files.
- **Reconciliation:** reuse a complete validation artifact only when its key and
  checksum match the current candidate. Otherwise rerun validation; a repeated
  read-only deterministic check is safe.

#### Planner and reviewer invocations

- **Precondition:** the issue, plan scope, candidate fingerprint, prompt-schema
  version, backend, provider, and model identity are fixed.
- **Persisted intent:** logical invocation key, complete input fingerprint,
  backend identity, result-artifact path, and whether the invocation consumes an
  implementation attempt.
- **Idempotency:** result adoption is keyed by the logical invocation and exact
  input fingerprint. Retry counters are updated once per logical operation, not
  once per provider request.
- **Reconciliation:** adopt a complete structured result artifact only when its
  key and checksum match. Otherwise repeat the read-only invocation with fresh
  context under the same logical key. Provider billing may occur twice, but the
  workflow must not advance state or consume an attempt twice.

#### Issue, plan, and review comments

- **Precondition:** the content fingerprint and target issue are known.
- **Persisted intent:** repository, issue number, comment kind, content hash, and
  stable marker.
- **Idempotency:** each comment kind uses a stable hidden marker and is updated
  in place.
- **Reconciliation:** search comments for the marker. Adopt or update the single
  match, create only when none exists, and block when duplicate marked comments
  cannot be reconciled safely.

#### Checkpoint commit

- **Precondition:** the candidate is approved, the worktree tree fingerprint is
  fixed, and `HEAD` equals the expected step-baseline parent.
- **Persisted intent:** expected parent SHA, tree SHA, commit message, step ID,
  and operation identifier.
- **Idempotency:** include `Investory-Operation-Id: <operation_id>` as a commit
  trailer and require the expected parent and tree.
- **Reconciliation:** search `HEAD` and reachable local commits for the matching
  trailer, parent, and tree. Adopt its SHA when found. Create the commit only
  when `HEAD` still equals the expected parent and the tree still matches. Block
  on any conflicting commit or tree.

#### Final history rewrite

- **Precondition:** final validation and review are approved; the issue baseline,
  checkpoint tip, final tree, target branch, and final message are fixed.
- **Persisted intent:** those values plus the finalization operation identifier.
- **Idempotency:** the final commit must have the issue baseline as parent, the
  approved final tree, and an `Investory-Operation-Id` trailer. Prefer creating
  the commit object first and changing the branch with an atomic compare-and-set
  ref update rather than a multi-command soft reset.
- **Reconciliation:** if the branch already points to the matching final commit,
  adopt it. If it still points to the checkpoint tip, complete the atomic ref
  update. If it points elsewhere or the approved tree cannot be reproduced,
  block. Never repeat a destructive reset without checking the branch ref.

#### Branch push

- **Precondition:** the local final commit SHA is confirmed and the expected
  remote old SHA has been recorded.
- **Persisted intent:** remote, branch ref, expected old SHA, target SHA, and push
  operation identifier.
- **Idempotency:** push the exact target SHA with lease protection against the
  expected remote old SHA.
- **Reconciliation:** query the remote ref. Target SHA means success and is
  adopted; expected old SHA means the push may be retried; any third SHA means
  divergence and blocks the workflow.

#### Draft pull-request upsert

- **Precondition:** the remote issue branch points to the confirmed target SHA.
- **Persisted intent:** repository, base branch, head branch, target SHA, draft
  state, body fingerprint, and stable operation marker.
- **Idempotency:** `(repository, base, head)` plus the stable marker identifies
  one logical pull request.
- **Reconciliation:** query GitHub by head and base before creating anything.
  Adopt and update the single matching PR, create only when none exists, and
  block when multiple candidates exist. A missing PR number in checkpoint state
  is not evidence that creation failed.

#### Dev Container lifecycle

- **Precondition:** the workspace and deterministic issue-specific project name
  are known.
- **Persisted intent:** project name, workspace, requested action, and operation
  identifier.
- **Idempotency:** start and stop target the same deterministic project name.
- **Reconciliation:** inspect the runtime before starting or stopping; adopt the
  observed running or stopped state when it already matches the request.

### Required uncertain-completion outcomes

The following crash boundaries must have explicit tests:

- coder changed files but crashed before returning
- validation finished but checkpoint persistence failed
- checkpoint commit succeeded but its SHA was not persisted
- final history rewrite changed the branch before confirmation was persisted
- push succeeded but remote success was not persisted
- PR creation or update succeeded but PR metadata was not persisted

In every case, resume must recover by observation and adoption or block on
divergence. It must not duplicate attempts, commits, pushes, comments, or pull
requests.

## State model

`app/state.py` defines the persisted `WorkflowState` contract using `TypedDict`
and string status aliases. Every field written by a graph node must
be declared there and initialized by the graph entry point.

Important state groups include issue metadata, workflow status, repository
context, plan and steps, workspace and branch, attempt counters, environment
result, validation result, review result, commits, PR metadata, CI placeholders,
and blocked-stage information.

## Project layout

```text
app/
├── __main__.py
├── cli.py
├── graph.py
├── state.py
├── github_client.py
├── workspace.py
├── side_effects.py
├── repository_context.py
├── test_runner.py
└── agents/
    ├── planner.py
    ├── coder.py
    └── reviewer.py
```

### Module responsibilities

- `app/__main__.py` — package entry point for `python -m app`
- `app/cli.py` — argument parsing, initial workflow state, checkpoint resume,
  and top-level workflow invocation
- `app/graph.py` — graph nodes, conditional routing, retries, step progression,
  checkpoints, finalization, push, PR handling, and terminal cleanup
- `app/state.py` — persisted workflow contract
- `app/github_client.py` — GitHub App authentication, issues, comments, and PRs
- `app/workspace.py` — cloning, branches, Git identity, checkpoints, history
  rewriting, and authenticated push
- `app/side_effects.py` — deterministic remote-operation IDs, prepared intent,
  completion evidence, and reconciliation helpers
- `app/repository_context.py` — bounded, read-only planning context
- `app/test_runner.py` — Dev Container lifecycle and deterministic validation

Provider-specific agent behavior belongs in `app/agents`, not in graph policy.

## Agent responsibilities

### Planner

Produces a structured `ImplementationPlan` containing the goal, assumptions,
open questions, issue-level acceptance criteria, ordered steps, step criteria,
validation expectations, affected areas, dependencies, and explicit exclusions.
It does not edit files.

### Coder

Implements only the current step, follows repository instructions, preserves or
extends tests, avoids unrelated changes, and leaves edits in the workspace. It
does not commit, push, or create a PR.

### Reviewer

Runs after deterministic validation and returns `approved`,
`changes_required`, or `review_failure`. It does not edit files. Every review
uses a fresh invocation and excludes coder reasoning. The same reviewer backend
may perform step and whole-plan reviews, provided each invocation satisfies the
review-independence policy above. When model separation is not established, the
result is a secondary review rather than an independent review.
