# Investory Orchestrator

Investory Orchestrator turns a GitHub issue that an operator has manually
confirmed as agent-ready into a validated draft pull request. It is a workflow
and policy layer around coding agents, Git, GitHub, Dev Containers, and
deterministic project validation.

The orchestrator does not merge pull requests automatically. Human review
remains the final approval step.

## Current limitations

- **Issue readiness is not validated automatically.** Before starting a run,
  the operator must manually confirm that the issue satisfies the agent-ready
  contract below. The current CLI does not reject an invalid issue before
  workspace creation or planner invocation.
- no major workflow capability is yet marked **VERIFIED**
- issues are started manually from the CLI
- there is no `agent-ready` queue runner yet
- blocked output can still be verbose
- graph routing needs dedicated automated tests
- GitHub Actions is not yet the final independent validation gate
- provider backends are not yet exposed through a common agent interface
- Codex execution depends on available authentication and usage quota

See [`ROADMAP.md`](ROADMAP.md) for planned stabilization and automation work.

## Capability status markers

Major workflow capabilities use these status markers:

- **VERIFIED** — implemented and proven by repeatable end-to-end tests
- **IMPLEMENTED** — an executable code path exists, but end-to-end verification
  is still pending
- **PARTIAL** — only part of the capability exists or an operator must complete
  part of the workflow manually
- **PLANNED** — intended behavior without a complete executable code path

No major workflow capability is currently marked **VERIFIED**. `ROADMAP.md` is
the authoritative source for capability and verification status.

## Delivery boundary

The project deliberately separates two delivery stages:

- **Supervised MVP** — a human starts a clear issue, the orchestrator plans and
  implements it step by step, runs deterministic validation and automated
  review, preserves resumable checkpoints, pushes the branch, and creates or
  updates a draft pull request.
- **Operational hardening** — strict automatic issue-contract validation,
  failed-attempt artifact preservation, clean-workspace retry isolation,
  provisional checkpoint approval, whole-plan architectural review, cross-step
  integration repair, and final commit-history rewriting.

Hardening capabilities may already be implemented and enabled in the current
workflow. They do not expand the supervised MVP completion criteria. They must
be verified before the orchestrator is considered safe for unattended or
production operation.

## Current workflow — IMPLEMENTED

The current issue-to-draft-PR path includes both supervised MVP behavior and
implemented hardening. The diagram describes the executable workflow, not the
MVP boundary. End-to-end verification is still pending.

```text
operator-approved GitHub issue
→ issue workspace and branch
→ bounded repository context
→ structured implementation plan
→ Dev Container startup
→ implement one plan step
→ deterministic validation
→ step review
→ checkpoint commit
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

Checkpoint commits are local recovery boundaries. Only the final logical commit
is pushed after the complete implementation passes validation and whole-plan
review.

## Agent-ready issue contract — PARTIAL · HARDENING

For the current supervised workflow, **agent-ready** means manually reviewed
and approved by the operator. Automatic enforcement of the issue contract is
operational hardening.

The workflow depends on the issue defining the product outcome clearly enough
that the planner does not need to act as a product manager.

An issue is considered **agent-ready** only when it contains the required
sections below and all material product decisions have already been made.

The structure below defines the operator's manual preflight. The orchestrator
does not currently validate or reject the issue before workspace creation or
planner invocation. Automatic issue-contract validation and rejection are
**PLANNED** and should run immediately after loading the issue.

### Required issue structure

```markdown
## Goal

Describe the observable product outcome.

## Context

Explain why the change is needed and describe relevant existing behavior.

## Product decisions

State product, business, compatibility, and UX decisions that the agent must
not make independently.

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

### Additional requirements for bugs

Bug issues must also define:

```markdown
## Reproduction

1. ...
2. ...

## Current behavior

Describe what happens now.

## Expected behavior

Describe what should happen instead.
```

Reproduction steps may be replaced by a deterministic failing test when that is
the clearest and most reliable reproduction.

### Contract rules

The issue author owns product intent. The agents own implementation within the
declared constraints.

The following information is mandatory:

- a specific, observable goal
- explicit in-scope and out-of-scope boundaries
- testable acceptance criteria
- expected validation or test coverage
- permission or prohibition for migrations
- permission or prohibition for breaking changes
- permission or prohibition for dependency and configuration changes
- reproduction, current behavior, and expected behavior for bugs

Implementation details are optional unless they represent a required
architectural or compatibility decision.

Examples of product decisions:

- whether an existing API must remain backward compatible
- which user-visible behavior is correct
- whether historical data must be migrated
- whether an operation should fail or degrade gracefully
- which roles or users may access a capability

Examples of implementation details:

- class names
- helper method structure
- internal package placement when repository conventions already define it
- choice between equivalent internal algorithms

### Rejection conditions

An issue should not be executed when:

- the goal is absent, vague, or only says to improve, fix, or test something
- acceptance criteria are missing or cannot be observed
- scope boundaries are missing
- a required product decision is delegated to the planner or coder
- change permissions are unspecified
- a bug cannot be reproduced and has no deterministic failing test
- the body contains unresolved placeholders such as `TBD`
- requirements contradict each other

**Automatic preflight status: PLANNED.** The intended flow is:

```text
load issue
→ validate issue contract
├── invalid → stop and report missing or conflicting fields
└── valid
    → prepare workspace
    → collect repository context
    → planner
```

**Queue enforcement status: PLANNED.** The future queue runner must require
both:

```text
agent-ready label
AND
valid issue contract
```

The label is human approval to execute the issue. It must not bypass contract
validation.

## Command line — IMPLEMENTED

**Precondition:** the operator has manually checked the issue against the
agent-ready contract above. The CLI does not perform this validation yet.

Run a new workflow:

```bash
docker compose run --rm orchestrator   python -m app.graph --issue <number>
```

Resume a blocked workflow:

```bash
docker compose run --rm orchestrator   python -m app.graph --issue <number> --resume
```

A stable LangGraph thread ID is derived from the issue number, so a resumed run
loads the saved checkpoint for the same issue.

## Workflow behavior

### Planning phase — IMPLEMENTED

The planning path exists; repeatable end-to-end verification is pending.

1. Load the issue title and body from GitHub.
2. Prepare or reuse `workspaces/issue-<number>`.
3. Check out `agent/issue-<number>`.
4. Collect bounded repository context.
5. Ask the planner for a structured plan.
6. Publish the plan to the issue when enabled.
7. Stop when the planner reports unresolved product questions.

Repository-specific questions such as build tool, package roots, and test
conventions should normally be answered from repository context rather than
asked of the user.

### Implementation phase — IMPLEMENTED

The multi-step implementation path exists; repeatable end-to-end verification
is pending.

For every plan step:

1. Mark the step as in progress.
2. Run the coder with the issue, current step, previous validation output, and
   reviewer feedback.
3. Run the repository validation script inside the Dev Container.
4. Retry the coder when validation fails.
5. Run the reviewer when validation succeeds.
6. Retry the coder when the reviewer requests changes.
7. Create a local checkpoint commit for the approved step.

The retry count is bounded by `MAX_ATTEMPTS`.

### Retry semantics — IMPLEMENTED

Retries are scoped to the current implementation step. `MAX_ATTEMPTS` is a
per-step limit, shared by validation and review repair loops.

One implementation attempt means:

```text
coder produces a candidate
→ deterministic validation
→ automated review when validation succeeds
```

The attempt is consumed once the coder has produced a candidate. A validation
failure and a reviewer request for changes both consume the same attempt; they
do not maintain separate counters.

Infrastructure failures should block the workflow without consuming an
implementation attempt. Examples include:

- the coding backend cannot start
- authentication or usage quota is unavailable
- the Dev Container cannot start
- the reviewer service is unavailable
- the coder times out before producing a candidate

#### Retry workspace isolation — IMPLEMENTED · HARDENING

Clean-workspace retry isolation is operational hardening. The supervised MVP
requires bounded retries, but does not use isolation as an MVP completion gate.

Every step must establish a baseline at the last approved commit:

```text
step baseline = HEAD when the step starts
```

Every retry must start from that baseline rather than editing the previous
failed candidate in place:

```text
step baseline
→ coder attempt
→ validation
→ review
├── approved
│   → commit the step
└── failed
    → preserve failed patch and diagnostics
    → reset workspace to the step baseline
    → start the next attempt
```

The next attempt receives the failed patch, validation output, and reviewer
findings as read-only diagnostic context. Failed source changes must not remain
in the working tree.

Before resetting, the orchestrator should preserve:

- the complete patch, including untracked files
- coder summary
- validation command, exit code, and output
- reviewer findings when review ran
- failure stage
- attempt number

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

After artifact preservation succeeds, the reset should be equivalent to:

```bash
git reset --hard <step-baseline-sha>
git clean -fd
```

The retry must remain constrained to:

- the same issue
- the same current plan step
- the original acceptance criteria
- the declared scope and change constraints
- the affected areas identified by the plan, unless broader changes are
  explicitly justified

A failed attempt is diagnostic input, not permission to broaden the solution.

#### Archived retry diagnostics — IMPLEMENTED · HARDENING

Failed-attempt artifact preservation is operational hardening.

The orchestrator archives the failed patch and diagnostics, resets the
workspace to the step baseline, and gives the next coder attempt the archived
patch as read-only context.

### Checkpoint commits and final logical history — IMPLEMENTED

Local checkpoint commits and resumable progress are part of the supervised MVP.
Provisional approval, whole-plan review, cross-step repair, and history
rewriting are operational hardening.

Step approval is provisional. A step-level reviewer proves that the current
candidate satisfies the current step; it does not prove that the architecture
will remain suitable after every later step is implemented.

Approved steps therefore create **checkpoint commits**:

- they provide recovery and resume boundaries
- they isolate later step work
- they remain local until whole-plan approval
- they may be rewritten or removed
- they are not treated as final architectural decisions

**Final whole-plan validation and review: IMPLEMENTED · HARDENING.** After the
final checkpoint, the orchestrator runs the complete validation suite again and
performs a whole-plan review over the diff from the original issue baseline.
End-to-end verification is pending.

The whole-plan reviewer checks:

- every issue-level acceptance criterion
- interactions between implementation steps
- abstractions invalidated by later work
- duplicated concepts and compensating workarounds
- public API and domain-model consistency
- migrations, configuration, and compatibility
- integration-level test coverage
- unrelated scope growth

**Isolated integration repair: IMPLEMENTED · HARDENING.** When final
validation fails or the whole-plan reviewer requests changes, an integration
repair attempt may revise code introduced by any checkpointed step.
The ordinary step boundaries no longer restrict that repair, but the original
issue scope and acceptance criteria still apply.

Whole-plan repair attempts use the same isolation rule:

```text
checkpoint tip
→ integration repair
→ final validation
→ whole-plan review
├── failed
│   → archive patch and diagnostics
│   → reset to checkpoint tip
│   → retry
└── approved
```

`MAX_FINAL_ATTEMPTS` bounds whole-plan repair attempts and defaults to
`MAX_ATTEMPTS`.

**Final logical history rewrite: IMPLEMENTED · HARDENING.** After final
approval, checkpoint commits are replaced with one logical commit:

```text
git reset --soft <issue-baseline-sha>
git add --all
git commit -m "Implement #<issue-number>: <issue-title>"
```

Only this final logical history is pushed to the draft pull request. Checkpoint
commit SHAs remain in workflow state for diagnostics, but they are no longer
reachable from the issue branch after finalization.

### Completion phase — IMPLEMENTED

Branch push and create-or-update draft PR handling define the supervised MVP
outcome. Final whole-plan review, integration repair, and history rewriting are
hardening stages in the current implementation. Repeatable end-to-end
verification, including existing-PR reuse, is pending.

After all steps are checkpointed:

1. Run final whole-plan validation.
2. Run the final whole-plan reviewer.
3. Repair integration findings when required.
4. Replace checkpoint commits with the final logical commit.
5. Push `agent/issue-<number>`.
6. Find an existing open pull request for that branch.
7. Update it, or create a new draft pull request.
8. Mark the workflow completed only after the pull request operation succeeds.
9. Stop the Dev Container environment.

### Blocked workflows and stage-aware resume — IMPLEMENTED

Checkpoint persistence and stage-aware resume paths exist. Route-by-route
end-to-end verification is pending.

A workflow becomes blocked when an environment, coder, reviewer, push, or pull
request stage cannot continue safely.

The checkpoint preserves:

- issue and branch
- workspace path
- implementation plan
- current and completed steps
- retry count
- validation output
- review output
- failed stage
- blocked reason
- pull request metadata

Resume is stage-aware. For example, a failed push resumes at the push stage,
while a failed coder attempt resumes at the implementation stage.

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

## Modules

### `app/graph.py`

Defines the LangGraph workflow and the CLI entry point.

Responsibilities:

- creates graph nodes and conditional routes
- loads and updates `WorkflowState`
- coordinates planner, coder, validation, and reviewer
- advances plan steps
- enforces retry limits
- records blocked stages
- resumes saved checkpoints
- commits completed steps
- pushes the branch
- creates or updates the draft pull request
- starts cleanup on terminal paths

This module should contain orchestration decisions, not provider-specific agent
implementation details.

### `app/state.py`

Defines the persisted workflow contract using `TypedDict` and literal status
types.

Important state groups:

- issue metadata
- workflow status
- repository context
- plan and normalized steps
- workspace and branch
- retry counters
- environment result
- validation result
- review result
- commit SHA
- pull request metadata
- CI placeholders
- blocked stage and reason

Any field written by a graph node should be declared here and initialized in
`graph.py`.

### `app/github_client.py`

Wraps GitHub App authentication and PyGithub operations.

Responsibilities include:

- obtaining an installation token
- loading GitHub issues
- posting issue comments
- resolving the configured repository
- finding an open PR by branch
- creating draft pull requests
- reading pull request information

Git push authentication is handled separately by `workspace.py` using the
short-lived GitHub App token.

### `app/workspace.py`

Manages the issue-specific Git workspace.

Responsibilities:

- clone the target repository
- create or reuse `agent/issue-<number>`
- mark mounted repositories as safe Git directories
- configure the automated Git identity
- create local checkpoint commits for approved steps
- rewrite checkpoint history into one final logical commit
- push the issue branch with GitHub App authentication

Existing workspaces are preserved so blocked workflows can be inspected and
resumed.

### `app/repository_context.py`

Builds a bounded, read-only summary for the planner.

The collector includes:

- top-level repository tree
- `AGENTS.md`
- `README.md`
- Maven or Gradle build files
- wrapper scripts when present
- existing Java package declarations
- a small set of representative tests

Large files and directory trees are truncated. Generated directories such as
`.git`, `target`, `build`, and `node_modules` are excluded.

The collected text is planning context only; this module does not edit the
target repository.

### `app/test_runner.py`

Controls the target repository's Dev Container and validation scripts.

Responsibilities:

- start the Dev Container environment
- execute project validation inside the environment
- stop the environment
- return structured success, exit code, and output values

The graph distinguishes:

- environment failure
- project validation failure
- validation success

Validation is authoritative and does not modify source code.

## Agents

### Planner — `app/agents/planner.py`

Converts the issue and repository context into an `ImplementationPlan`.

The structured plan contains:

- overall goal and summary
- assumptions
- open questions
- issue-level acceptance criteria
- ordered implementation steps
- step requirements
- step acceptance criteria
- expected validation
- affected areas
- dependencies
- explicit out-of-scope items

The planner must not write code. It should ask questions only when a product
decision cannot be inferred safely from the issue or repository.

The Markdown renderer adds a stable marker so plan comments can later be made
idempotent.

### Coder — `app/agents/coder.py`

Implements one plan step in the current workspace.

Inputs include:

- issue title and body
- current plan step
- attempt number
- previous validation output
- reviewer findings
- current Git diff

Rules enforced by the prompt:

- inspect repository instructions first
- implement only the current step
- avoid unrelated changes
- preserve and extend tests
- do not commit, push, or create a PR
- leave edits in the workspace for the orchestrator

The current backend invokes Codex CLI. Codex authentication and quota are
external runtime concerns; a coder failure is normalized into workflow state.

### Reviewer — `app/agents/reviewer.py`

Reviews the implementation after project validation succeeds.

The reviewer compares:

- issue requirements
- current plan step
- acceptance criteria
- current Git diff
- validation output

It returns structured status:

```text
approved
changes_required
review_failure
```

Blocking findings route back to the coder when retries remain. The reviewer does
not edit files.

The same reviewer backend also performs a final `whole_plan` review. That review
receives the complete plan, final validation output, and the full diff from the
issue baseline. It may require redesign of an earlier checkpoint when the
integrated implementation is inconsistent or unnecessarily complex.

## Configuration

The exact environment file depends on the selected agent providers. Common
settings include:

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

Planner and reviewer model settings may use OpenAI, Azure OpenAI, or an
OpenAI-compatible gateway such as Open WebUI.

When the coder uses Codex CLI, mount the authenticated host Codex directory into
the orchestrator container user's home. For a root container user:

```yaml
services:
  orchestrator:
    volumes:
      - ${HOME}/.codex:/root/.codex
```

Do not commit API keys, GitHub private keys, Codex credentials, or generated
installation tokens.

## Target repository requirements

The target repository should provide:

- an `AGENTS.md` file with repository-specific agent rules
- a `.devcontainer/devcontainer.json`
- scripts used by the orchestrator, including the validation entry point
- deterministic validation that exits non-zero on failure
- a default branch compatible with the configured PR base

The current Investory setup uses a Dev Container and Maven-based validation.

## Operational checks

Compile the orchestrator modules:

```bash
docker compose run --rm orchestrator   python -m py_compile   app/state.py   app/github_client.py   app/workspace.py   app/repository_context.py   app/test_runner.py   app/agents/planner.py   app/agents/coder.py   app/agents/reviewer.py   app/graph.py
```

Verify Codex inside the orchestrator container before running an issue:

```bash
docker compose run --rm orchestrator   codex exec "Reply only with: container Codex works"
```

Inspect an issue workspace:

```bash
cd workspaces/issue-<number>
git status
git log --oneline --decorate -10
```
