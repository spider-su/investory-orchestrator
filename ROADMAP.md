# Investory Orchestrator Roadmap

This document contains only authoritative status and unfinished work. Current
behavior and operating procedures belong in `docs/`.

## Delivery boundary

- **Supervised MVP:** human-started, human-supervised issue-to-draft-PR loop.
- **Operational hardening:** safeguards required for reliable unattended or
  production operation.

Implemented hardening may remain enabled, but it does not enlarge the MVP
completion gate.

## Capability status

| Capability | Implemented | Tested E2E | Production-ready |
| --- | --- | --- | --- |
| **MVP:** GitHub issue loading | Yes | No | No |
| **MVP:** Repository-aware structured planning | Yes | No | No |
| **MVP:** Isolated issue workspace and branch | Yes | No | No |
| **MVP:** Dev Container startup and validation | Yes | No | No |
| **MVP:** Multi-step execution | Yes | No | No |
| **MVP:** Validation repair loop | Yes | No | No |
| **MVP:** Reviewer repair loop | Yes | No | No |
| **MVP:** Retry exhaustion and blocked state | Yes | No | No |
| **MVP:** Local checkpoint commits | Yes | No | No |
| **MVP:** Stage-aware resume routing | Yes | No | No |
| **MVP:** Issue branch push | Yes | No | No |
| **MVP:** Draft PR creation or update | Yes | No | No |
| **Hardening:** Failed-attempt artifact preservation | Yes | No | No |
| **Hardening:** Clean-workspace retry isolation | Yes | No | No |
| **Hardening:** Provisional checkpoint approval | Yes | No | No |
| **Hardening:** Whole-plan architectural review | Yes | No | No |
| **Hardening:** Cross-step integration repair | Yes | No | No |
| **Hardening:** Final logical history rewrite | Yes | No | No |
| **Hardening:** Automatic issue-contract validation | No | No | No |
| **Hardening:** Complete graph and routing test coverage | No | No | No |
| **Hardening:** Complete side-effect intent and resume reconciliation | No | No | No |
| **Hardening:** Final-review independence before READY | Yes | No | No |
| **Hardening:** GitHub Actions polling and repair | Yes | No | No |
| **Hardening:** Persistent task aggregate and guarded transitions | Yes | No | No |
| **Hardening:** Prompt and issue task intake | Yes | No | No |
| **Hardening:** Per-task Git worktrees | Yes | No | No |
| **Hardening:** Queue status and resource limits | Yes | No | No |
| **Hardening:** CI polling and bounded repair | Yes | No | No |
| **Hardening:** Post-CI independent final review | Yes | No | No |
| **Hardening:** Pluggable agent backends | No | No | No |
| **Hardening:** `ready_to_develop` label polling and acknowledgement | Yes | No | No |
| **POC:** PostgreSQL-backed tasks and LangGraph checkpoints | Yes | No | No |
| **POC:** Repository configuration CRUD and task dashboard | Yes | No | No |
| **POC:** k3s scheduler to devMac SSH worker dispatch | Yes | No | No |
| **POC:** READY/BLOCKED GitHub issue mention | Yes | No | No |
| **POC:** GitHub Projects tracking and priority synchronization | No | No | No |

Definitions:

- **Implemented:** an executable code path exists.
- **Tested E2E:** repeatable end-to-end scenarios pass without manual repository
  repair.
- **Production-ready:** independent validation, recovery, observability, and
  operational coverage are sufficient for unattended use.

The supervised MVP is complete when every **MVP:** row is `Yes` for both
Implemented and Tested E2E. Production readiness requires applicable hardening
to be implemented and verified.

Update this table only when implementation or verification evidence changes.

Focused tests cover current graph routes, remote push/PR reconciliation, task
transitions, CI classification, final READY gates, and worktree setup. The
table remains conservative: no full task lifecycle has been tested end to end.

## Priority 1 — Verify the supervised MVP

Run these scenarios repeatedly against real or deliberately constructed issues:

1. Successful multi-step issue through draft PR.
2. Validation failure repaired on a later attempt.
3. Reviewer finding repaired on a later attempt.
4. Retry exhaustion produces a resumable blocked state.
5. Resume continues the saved step without repeating completed steps.
6. Existing open PR is updated instead of duplicated.

Done when all six scenarios pass repeatedly without manual repository repair,
a completed run produces a reviewable draft PR, and blocked runs preserve enough
state for safe resume.

## Priority 2 — Complete safety and validation hardening

- Add automatic issue-contract validation before workspace creation and
  planning.
- Add graph tests for every conditional route and retry boundary.
- Test resume from environment, coder, reviewer, validation, push, and PR
  failures.
- Persist write-ahead intent before every workspace, commit, history rewrite,
  push, comment, and PR side effect.
- Define deterministic operation IDs, expected before-and-after state, and
  durable external references for each side-effecting node.
- Reconcile local Git, remote refs, comments, and PRs before replaying an
  operation whose completion is uncertain.
- Add crash-boundary tests for dirty coder worktrees, completed validation with
  missing state, commits with missing SHA state, interrupted finalization,
  successful pushes with missing confirmation, and PR upserts with missing
  metadata.
- Verify failed-attempt artifact preservation and clean retry isolation.
- Verify fresh, read-only reviewer invocations without coder session history or
  hidden reasoning across both graph and post-CI review paths.
- Record coder and reviewer backend, provider, and model identity in workflow
  state and pull-request evidence.
- Require different coder and reviewer model identities before labeling an LLM
  result independent; otherwise label it secondary review.
- Verify whole-plan repair and final history rewriting.
- Run repeated live verification for GitHub Actions polling, repair, and
  final-review gating.
- Keep PRs draft and retain human merge approval.

## Priority 3 — Improve diagnostics and recovery

- Replace raw blocked output with concise issue, step, stage, attempt, reason,
  relevant log lines, and resume command.
- Update one stable issue comment instead of creating duplicates.
- Add `--status`, `--restart`, and explicit resume targets.
- Define checkpoint, workspace, and run-artifact retention policies.
- Add structured JSON logs and per-node timing.
- Report backend, token, cost, quota, and execution limits.

## Priority 4 — Simplify agent integration

- Introduce a common planner, coder, and reviewer backend interface.
- Support role-specific prompts, bounded execution, structured output,
  normalized errors, and optional repository write access.
- Replace repeated prompt concatenation with prompt builders.
- Replace ad hoc repository context with structured metadata for languages,
  frameworks, modules, package roots, entry points, documentation, and
  validation commands.

## Priority 5 — Verify automatic issue queue intake

The scheduler now polls enabled repositories for `ready_to_develop`, queues
each repository/issue pair idempotently, removes the authorization label after
acknowledgement, and posts a stable status comment. Existing concurrency limits,
CI polling, bounded repairs, independent final review, and manual merge gates
remain in force. Next verify a real labeled issue through the deployed Mac
runner and confirm the ready-for-review mention and post-merge completion.

## Later work

- scheduled polling
- webhook startup
- dependency-aware plans
- optional parallel independent steps
- optional parallel workers
- direct read-only repository exploration by planner and reviewer

## Recommended delivery order

1. Pass all supervised MVP scenarios.
2. Add graph and resume integration tests.
3. Implement write-ahead side-effect intent and uncertain-completion
   reconciliation.
4. Add automatic issue-contract validation.
5. Improve blocked-state reporting and recovery controls.
6. Add independent GitHub Actions validation.
7. Verify retry isolation, whole-plan repair, and history rewriting.
8. Introduce pluggable agent backends and prompt builders.
9. Add structured repository metadata and operational telemetry.
10. Verify automatic `ready_to_develop` intake against the deployed runner.
11. Add webhook intake and optional parallelism if polling is insufficient.

## POC — k3s scheduler and devMac execution

Initial implementation exists in `k8s/`, `app/dashboard.py`, and the Mac
worker dispatch path. It is not deployment-verified. Follow
[`docs/k3s-poc.md`](docs/k3s-poc.md) to provision credentials, publish an
immutable image, deploy, and run the first issue. GitHub Projects discovery
and priority synchronization remain blocked on access to the selected project
and its priority field.
