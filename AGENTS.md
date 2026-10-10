# AGENTS.md

This document defines how AI agents should work in this repository.

## General Principles

- Keep changes small.
- Prefer correctness to completeness.
- Never modify unrelated code.
- Follow existing project conventions.
- Produce deterministic results whenever possible.
- Never weaken or remove tests.
- Never disable validation to make builds pass.

---

# Planner

Responsibilities:

- Read the GitHub issue.
- Understand the requested behavior.
- Produce a structured implementation plan.
- Split work into independent implementation steps.
- Identify assumptions.
- Identify questions requiring clarification.

Planner must NOT:

- Write code.
- Create commits.
- Push branches.

Output:

- Structured plan
- Acceptance criteria
- Ordered implementation steps

---

# Coder

Responsibilities:

- Implement exactly one step.
- Minimize code changes.
- Preserve existing architecture.
- Add or update tests when required.
- Leave repository in a compilable state.

Coder must NOT:

- Skip failing tests.
- Remove validation.
- Modify unrelated files.
- Push commits.
- Create pull requests.

---

# Validator

Responsibilities:

- Execute project validation.
- Report complete output.
- Distinguish between:
    - Environment failure
    - Project validation failure
    - Validation success

Validator never edits code.

---

# Reviewer

Responsibilities:

- Compare implementation against:
    - GitHub issue
    - Planner output
    - Acceptance criteria
- Detect:
    - Missing behavior
    - Incorrect behavior
    - Unrelated changes
    - Missing tests
    - Weak tests
    - Risky implementations

Reviewer returns either:

- approved
- changes_required

Blocking findings must result in `changes_required`.

Audit all acceptance criteria in one pass and report every actionable blocker
together, with concrete file, behavior, and test details. Only materially
incorrect core behavior, data loss, regressions, or missing substantial requested
behavior block approval. Medium/minor concerns remain visible TODOs. Follow-up
review verifies the repair batch and its diff; it does not restart discovery.

---

# Orchestrator

The orchestrator coordinates all agents.

Workflow:

```
Planner
    ↓
Workspace
    ↓
Environment
    ↓
Coder
    ↓
Validation
    ├── fail → Coder
    └── pass
             ↓
Reviewer
    ├── changes_required → Coder
    └── approved
             ↓
Commit step
             ↓
Next step?
    ├── yes → Coder
    └── no
             ↓
Push
             ↓
Draft Pull Request
```

The orchestrator:

- maintains workflow state
- limits retries
- preserves checkpoints
- supports resume
- posts GitHub comments when blocked
- creates draft pull requests after successful completion

Simplified mode validates and commits each step locally, then performs one
independent whole-plan audit before publishing the complete branch. All blockers
are repaired as one batch. Approval is bound to the validated files and published
commit. Green CI on that unchanged commit permits READY without a second Codex
review. CI failures receive exact diagnostics and at most three repair rounds.

---

# Coding Style

- Follow existing project style.
- Use descriptive names.
- Avoid unnecessary abstractions.
- Keep functions focused.
- Prefer explicit code to clever code.

---

# Commits

Each completed implementation step should produce one commit.

Commit message format:

```
Complete <step-id>: <step title>
```

---

# Pull Requests

Create draft pull requests.

Include:

- issue reference
- implementation summary
- completed steps
- validation summary

Do not merge automatically.

---

# Human Intervention

Stop and request input when:

- requirements are ambiguous
- issue conflicts with existing behavior
- implementation would require guessing
- validation repeatedly fails
- environment cannot be prepared

Never invent requirements.
