# Investory Orchestrator

Investory Orchestrator turns a GitHub issue that an operator has manually
confirmed as agent-ready into a validated draft pull request. It coordinates
coding agents, Git, GitHub, Dev Containers, deterministic validation, and
review policy.

It does not merge pull requests automatically. Human review remains the final
approval step.

## Current status

The supervised issue-to-draft-PR workflow is implemented, but repeatable
end-to-end verification is still pending. The project is not yet intended for
unattended or production operation.

[`ROADMAP.md`](ROADMAP.md) is the authoritative source for implementation,
verification, and production-readiness status.

## What it does

```text
operator-approved GitHub issue
→ repository-aware plan
→ step-by-step implementation
→ deterministic validation and automated review
→ resumable local checkpoints
→ final branch
→ draft pull request
→ human review
```

The current executable workflow also includes several operational-hardening
features, such as isolated retries, whole-plan review, integration repair, and
final history rewriting. These features are described in
[`docs/architecture.md`](docs/architecture.md).

## Delivery boundary

- **Supervised MVP:** a human selects and approves the issue, starts the run,
  reviews failures, and performs final pull-request review.
- **Operational hardening:** safeguards required before unattended use,
  including automatic issue validation, complete routing tests, independent CI,
  structured recovery, and queue execution.

Implemented hardening does not expand the supervised MVP completion gate, but
it must be verified before production use.

## Quick start

Before running the orchestrator, manually verify the issue against
[`docs/issue-contract.md`](docs/issue-contract.md). The CLI does not yet reject
an invalid issue before workspace creation or planner invocation.

Run a new workflow:

```bash
docker compose run --rm orchestrator \
  python -m app.graph --issue <number>
```

Resume a blocked workflow:

```bash
docker compose run --rm orchestrator \
  python -m app.graph --issue <number> --resume
```

A stable LangGraph thread ID is derived from the issue number, so resume loads
the saved checkpoint for that issue.

## Current limitations

- Issue readiness is enforced manually, not by the executable workflow.
- No major workflow capability is yet marked **Verified E2E**.
- Issues are started manually from the CLI.
- There is no `agent-ready` queue runner.
- Graph routing and resume paths need dedicated automated tests.
- Remote branch push and draft-PR upsert persist write-ahead intent and
  reconcile uncertain completion before retrying. Local checkpoint commits and
  final history rewriting do not yet have equivalent operation records, so
  crashes around those local Git mutations can still require manual recovery.
- GitHub Actions is not yet the final independent validation gate.
- Reviewer independence is not yet enforced. The current configuration can use
  the same underlying model family for coding and review, and reviewer identity
  is not yet recorded as workflow evidence.
- Provider backends are not exposed through a common agent interface.
- Blocked-state reporting can still be verbose.
- Codex execution depends on available authentication and usage quota.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — workflow graph, state,
  retries, checkpoints, finalization, and module responsibilities
- [`docs/issue-contract.md`](docs/issue-contract.md) — manual agent-ready issue
  standard and future automatic preflight
- [`docs/operations.md`](docs/operations.md) — configuration, credentials,
  commands, validation, inspection, and recovery
- [`ROADMAP.md`](ROADMAP.md) — authoritative status and unfinished work
