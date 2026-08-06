# Investory Orchestrator

Investory Orchestrator is an AI-driven workflow that implements GitHub issues with minimal human intervention.

The orchestrator coordinates specialized agents that:

1. Read and understand a GitHub issue.
2. Create an implementation plan.
3. Prepare an isolated workspace.
4. Start the project's development environment.
5. Implement the solution.
6. Validate the project.
7. Review the implementation.
8. Push changes and create a draft pull request.

## Workflow
Investory Orchestrator turns a prepared GitHub issue into a validated draft pull
request. It is a workflow and policy layer around coding agents, Git, GitHub,
Dev Containers, and project validation.

The current implementation processes one issue at a time:

```text
Issue
  │
  ▼
Planner
  │
  ▼
Workspace
  │
  ▼
Environment
  │
  ▼
Coder
  │
  ▼
Validation
  ├── fail ──► Coder (retry)
  └── pass
         │
         ▼
Reviewer
  ├── changes required ─► Coder
  └── approved
         │
         ▼
Commit step
         │
         ▼
Next step?
  ├── yes ─► Coder
  └── no
         │
         ▼
Push branch
         │
         ▼
Draft Pull Request
```

## Components

- **Planner** – converts an issue into a structured implementation plan.
- **Coder** – modifies the repository to complete one implementation step.
- **Validator** – executes the project's validation commands.
- **Reviewer** – verifies implementation quality against the issue and plan.
- **GitHub Client** – communicates with GitHub using a GitHub App.
- **Workspace Manager** – creates and maintains isolated workspaces.
- **Environment Manager** – starts and stops Dev Containers.

## Running

```bash
python -m app.graph --issue <number>
```

Example:

```bash
python -m app.graph --issue 18
```

## Current Goals

- Reliable autonomous implementation
- Deterministic validation
- Repeatable workflows
- Safe retries
- Resume interrupted executions
- Draft pull requests for human review+GitHub issue
  → issue workspace and branch
  → bounded repository context
  → structured implementation plan
  → Dev Container startup
  → implement one plan step
  → project validation
  → automated review
  → commit approved step
  → repeat remaining steps
  → push branch
  → create or update draft PR
```

The orchestrator does not merge pull requests automatically. Human review remains
the final approval step.

## Command line

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

### Planning phase

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

### Implementation phase

For every plan step:

1. Mark the step as in progress.
2. Run the coder with the issue, current step, previous validation output, and
   reviewer feedback.
3. Run the repository validation script inside the Dev Container.
4. Retry the coder when validation fails.
5. Run the reviewer when validation succeeds.
6. Retry the coder when the reviewer requests changes.
7. Commit the approved step.

The retry count is bounded by `MAX_ATTEMPTS`.

### Completion phase

After all steps are approved:

1. Push `agent/issue-<number>`.
2. Find an existing open pull request for that branch.
3. Update it, or create a new draft pull request.
4. Mark the workflow completed only after the pull request operation succeeds.
5. Stop the Dev Container environment.

### Blocked workflows

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
- create one commit per approved step
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

## Current limitations

- issues are started manually from the CLI
- there is no `agent-ready` queue runner yet
- blocked output can still be verbose
- graph routing needs dedicated automated tests
- GitHub Actions is not yet the final independent validation gate
- provider backends are not yet exposed through a common agent interface
- Codex execution depends on available authentication and usage quota

See [`ROADMAP.md`](ROADMAP.md) for planned stabilization and automation work.
