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

After approval, checkpoint history is replaced with one logical commit:

```bash
git reset --soft <issue-baseline-sha>
git add --all
git commit -m "Implement #<issue-number>: <issue-title>"
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

## State model

`app/state.py` defines the persisted `WorkflowState` contract using
`TypedDict` and literal status values. Every field written by a graph node must
be declared there and initialized by the graph entry point.

Important state groups include issue metadata, workflow status, repository
context, plan and steps, workspace and branch, attempt counters, environment
result, validation result, review result, commits, PR metadata, CI placeholders,
and blocked-stage information.

## Project layout

```text
app/
├── graph.py
├── state.py
├── github_client.py
├── workspace.py
├── repository_context.py
├── test_runner.py
└── agents/
    ├── planner.py
    ├── coder.py
    └── reviewer.py
```

### Module responsibilities

- `app/graph.py` — graph nodes, conditional routing, retries, step progression,
  checkpoints, finalization, push, PR handling, and terminal cleanup
- `app/state.py` — persisted workflow contract
- `app/github_client.py` — GitHub App authentication, issues, comments, and PRs
- `app/workspace.py` — cloning, branches, Git identity, checkpoints, history
  rewriting, and authenticated push
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
