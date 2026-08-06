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
| **MVP:** Stage-aware resume | Yes | No | No |
| **MVP:** Issue branch push | Yes | No | No |
| **MVP:** Draft PR creation or update | Yes | No | No |
| **Hardening:** Failed-attempt artifact preservation | Yes | No | No |
| **Hardening:** Clean-workspace retry isolation | Yes | No | No |
| **Hardening:** Provisional checkpoint approval | Yes | No | No |
| **Hardening:** Whole-plan architectural review | Yes | No | No |
| **Hardening:** Cross-step integration repair | Yes | No | No |
| **Hardening:** Final logical history rewrite | Yes | No | No |
| **Hardening:** Automatic issue-contract validation | No | No | No |
| **Hardening:** Graph and routing test coverage | No | No | No |
| **Hardening:** Reviewer independence enforcement | No | No | No |
| **Hardening:** GitHub Actions independent validation | No | No | No |
| **Hardening:** Pluggable agent backends | No | No | No |
| **Hardening:** Sequential `agent-ready` queue | No | No | No |

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
- Verify failed-attempt artifact preservation and clean retry isolation.
- Enforce fresh, read-only reviewer invocations without coder session history or
  hidden reasoning.
- Record coder and reviewer backend, provider, and model identity in workflow
  state and pull-request evidence.
- Require different coder and reviewer model identities before labeling an LLM
  result independent; otherwise label it secondary review.
- Verify whole-plan repair and final history rewriting.
- Add GitHub Actions as an independent PR validation gate.
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

## Priority 5 — Add unattended queue execution

Add a sequential runner that:

- selects only issues labeled `agent-ready`
- also requires a valid automatic issue contract
- skips issues already running or represented by an open agent PR
- applies `agent-running`, `agent-blocked`, or `agent-completed`
- processes one issue at a time
- limits issues, failures, and execution time per run
- preserves blocked workspaces

Unattended queue execution remains blocked until automatic issue validation,
routing tests, independent CI, and recovery behavior are verified.

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
3. Add automatic issue-contract validation.
4. Improve blocked-state reporting and recovery controls.
5. Add independent GitHub Actions validation.
6. Verify retry isolation, whole-plan repair, and history rewriting.
7. Introduce pluggable agent backends and prompt builders.
8. Add structured repository metadata and operational telemetry.
9. Add the sequential `agent-ready` queue.
10. Add polling, webhooks, and optional parallelism.
