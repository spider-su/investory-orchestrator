# Agent-ready issue contract

The current CLI does not validate issue readiness automatically. Before
starting a run, the operator must review the issue against this contract.
Automatic rejection before workspace creation and planning is planned
operational hardening.

## Ownership

The issue author owns product intent. Agents own implementation choices inside
the declared scope and constraints.

An issue is agent-ready only when all material product, business,
compatibility, and UX decisions have already been made. The planner must not be
required to act as a product manager.

## Required issue structure

```markdown
## Goal

Describe the observable product outcome.

## Context

Explain why the change is needed and describe relevant existing behavior.

## Product decisions

State decisions the agent must not make independently.

- Intended user behavior:
- Compatibility expectations:
- Business or UX rules:

## Scope

### In scope

- ...

### Out of scope

- ...

## Acceptance criteria

- [ ] Observable and testable result
- [ ] Observable and testable result

## Validation

- Expected automated tests:
- Required manual checks:
- Existing validation that must remain passing:

## Change constraints

- Database migration allowed: yes/no
- Breaking API change allowed: yes/no
- Dependency changes allowed: yes/no
- Configuration changes allowed: yes/no

## Implementation notes

Optional technical guidance. The agent may choose another implementation when
it satisfies the product decisions, scope, constraints, and acceptance
criteria.
```

## Additional bug requirements

Bug issues must also include:

```markdown
## Reproduction

1. ...
2. ...

## Current behavior

Describe what happens now.

## Expected behavior

Describe what should happen instead.
```

A deterministic failing test may replace reproduction steps when it is the
clearest and most reliable reproduction.

## Mandatory information

Every executable issue must define:

- a specific, observable goal
- explicit in-scope and out-of-scope boundaries
- testable acceptance criteria
- expected validation or test coverage
- permission or prohibition for migrations
- permission or prohibition for breaking changes
- permission or prohibition for dependency changes
- permission or prohibition for configuration changes
- reproduction, current behavior, and expected behavior for bugs

Implementation details are optional unless they encode a required architecture,
compatibility, or product decision.

Examples of product decisions:

- whether an API must remain backward compatible
- which user-visible behavior is correct
- whether historical data must be migrated
- whether an operation fails or degrades gracefully
- which roles may access a capability

Examples of optional implementation details:

- class names
- helper-method structure
- internal package placement when repository conventions already determine it
- choice between equivalent internal algorithms

## Rejection conditions

Do not execute an issue when:

- the goal is absent or only says to improve, fix, or test something
- acceptance criteria are missing or cannot be observed
- scope boundaries are missing
- a required product decision is delegated to the planner or coder
- change permissions are unspecified
- a bug has neither reproduction steps nor a deterministic failing test
- unresolved placeholders such as `TBD` remain
- requirements contradict one another

## Planned automatic preflight

Automatic validation should execute immediately after issue loading:

```text
load issue
→ validate issue contract
├── invalid → stop and report missing or conflicting fields
└── valid
    → prepare workspace
    → collect repository context
    → planner
```

A future unattended queue must require both:

```text
agent-ready label
AND
valid issue contract
```

The label is human authorization to execute. It must not bypass contract
validation.
