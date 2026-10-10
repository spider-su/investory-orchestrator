# Ready-to-develop issue contract

Issues carrying the `ready_to_develop` label are queued automatically. The
workflow checks the description before workspace creation. When it is
unstructured, it wraps the original text in the standard sections, preserves
the original verbatim, and records conservative repository-convention defaults.
Formatting alone does not reject an issue or consume a Codex run.

## Ownership

The issue author owns product intent. Agents own implementation choices inside
the declared scope and constraints.

The issue author supplies the requested outcome. Agents resolve routine
technical choices from repository conventions, preserve compatibility, and
keep scope narrow. Only an empty issue or a material product, security, or
data-loss decision that cannot be inferred safely should block execution.

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

## Automatic formatting and validation

If a non-empty issue does not follow the required structure, the formatter adds
the canonical headings, derives the goal from the title, and puts the complete
original request under `## Original issue description`. It adds conservative
defaults for scope, acceptance criteria, validation, and change constraints,
without rewriting or removing original text. Constraints default to `no`
unless the request clearly requires that category of change. Bug labels receive
current/expected/reproduction headings; agents verify those details from the
preserved request, repository, and tests.

Preflight is:

```text
load issue
→ validate description
→ format it in place when needed, preserving original text
→ queue and retain `ready_to_develop` authorization
→ planner resolves routine details from the repository
→ stop only for a genuinely unsafe or irreversible missing decision
```

The `ready_to_develop` label is the execution authorization. The scheduler
updates the issue body when formatting is needed, posts a short formatting
notice, queues the normalized description, and removes the label as its
acknowledgement. Empty title/body and GitHub update failures remain operational
errors; they do not consume a coder round.
