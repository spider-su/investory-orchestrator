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
- Draft pull requests for human review