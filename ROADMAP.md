# Investory Orchestrator Roadmap

## Current MVP Scope

The MVP is complete when the orchestrator can:

1. Read a GitHub issue.
2. Produce a structured implementation plan.
3. Execute the plan step by step.
4. Let the coder retry after validation or review failures.
5. Commit each completed step.
6. Resume a blocked workflow from its checkpoint.
7. Push the issue branch.
8. Create or update a draft pull request.

## Remaining MVP Work

### 1. Multi-step execution

**Goal**

Execute every planner step independently and in order.

**Required**

- Store normalized steps in LangGraph state.
- Track `current_step`.
- Track `completed_steps`.
- Reset retry state before each step.
- Send only the current step to the coder and reviewer.
- Move to the next step after reviewer approval.
- Stop only after all steps are complete.

**Done when**

- A plan with at least two steps runs from start to finish.
- Each step is validated and reviewed separately.
- Later steps are not implemented early.

---

### 2. Retry loop

**Goal**

Allow the coder to fix validation and review failures.

**Required**

- Route `coder -> validation`.
- Route failed validation back to the coder.
- Pass validation output to the next coder attempt.
- Route reviewer changes back to the coder.
- Pass review findings to the next coder attempt.
- Enforce `MAX_ATTEMPTS`.
- Mark the workflow blocked when the limit is reached.

**Done when**

- A deliberately failing implementation is corrected on a later attempt.
- The workflow stops after the configured retry limit.
- Failure output is preserved in state.

---

### 3. Step commits

**Goal**

Create one commit for every approved implementation step.

**Required**

- Stage all files after reviewer approval.
- Commit with:

```text
Complete <step-id>: <step title>
```

- Save the commit SHA in the step state.
- Do not commit failed or unreviewed work.
- Handle steps that require no file changes.

**Done when**

- Git history contains one commit per completed step.
- Each step stores its commit SHA.
- No automatic commit is created before approval.

---

### 4. Resume support

**Goal**

Continue a blocked workflow without recreating its plan or workspace.

**Required**

- Use a stable thread ID:

```text
investory-issue-<number>
```

- Add CLI support:

```bash
python -m app.graph --issue <number> --resume
```

- Load the latest LangGraph checkpoint.
- Preserve:
  - workspace
  - branch
  - plan
  - current step
  - completed steps
  - retry count
  - validation output
  - review output
- Clear only the blocking error before resuming.
- Reject resume when no checkpoint exists.

**Done when**

- The process can be stopped after a blocked result.
- A later `--resume` invocation continues the same step.
- Completed steps are not repeated.

---

### 5. Draft pull request

**Goal**

Push the final branch and open a draft pull request.

**Required**

- Push:

```text
agent/issue-<number>
```

- Reuse an existing open PR for the same branch.
- Otherwise, create a draft PR to `main`.
- Include:
  - issue reference
  - plan summary
  - completed steps
  - local validation result
  - automated review result
- Store PR number and URL in state.

**Done when**

- A completed workflow creates or updates one draft PR.
- Re-running does not create duplicate PRs.
- The PR contains all completed steps.

---

## MVP Verification

Before declaring MVP complete, run these scenarios.

### Scenario A — Successful issue

- Issue contains two or more clear steps.
- All coder changes pass on the first attempt.
- Each step is committed.
- Branch is pushed.
- Draft PR is created.

### Scenario B — Validation retry

- First coder attempt causes a test failure.
- Validation output is sent back to the coder.
- Second attempt fixes the issue.
- Workflow continues normally.

### Scenario C — Reviewer retry

- Validation passes.
- Reviewer finds a missing acceptance criterion.
- Coder receives the finding.
- Updated implementation passes review.

### Scenario D — Retry exhaustion

- Validation keeps failing.
- Workflow stops after `MAX_ATTEMPTS`.
- Blocked reason is stored.
- Workspace and checkpoint remain available.

### Scenario E — Resume

- Start from Scenario D.
- Fix the blocking condition manually or adjust the issue.
- Run with `--resume`.
- Workflow continues from the saved step.

### Scenario F — Existing PR

- Run the workflow for a branch that already has an open PR.
- Existing PR is updated instead of duplicated.

---

## Current Status and Revised Priorities

The core issue-to-PR workflow is implemented:

- repository-aware planning
- isolated issue workspaces
- Dev Container startup and validation
- multi-step implementation
- validation and review retries
- per-step commits
- stage-aware checkpoints and resume
- branch push
- draft pull request creation or update

The remaining work is focused on proving the workflow with real issues,
simplifying agent integrations, and making unattended operation reliable.

### Priority 1 — Prove and stabilize the MVP

#### End-to-end verification

Run all six MVP scenarios against real or deliberately constructed issues.

Focus first on:

1. successful multi-step execution
2. validation repair
3. reviewer repair
4. retry exhaustion
5. stage-aware resume
6. existing pull request reuse

**Done when**

- All scenarios pass repeatedly without manual repository repair.
- A completed issue produces a reviewable draft PR.
- A blocked issue preserves enough state to resume safely.

#### Graph and routing tests

Add automated tests for:

- every conditional route
- retry boundaries
- step progression
- environment, coder, reviewer, push, and PR failures
- resume from every blocked stage
- completed status only after PR success

#### Blocked-state reporting

Replace raw command dumps with concise summaries containing:

- issue and current step
- failed stage
- attempt count
- short reason
- relevant final log lines
- resume command
- known retry time when available

Post or update one stable issue comment rather than creating duplicates.

#### GitHub Actions validation

- Run the complete validation suite on the PR.
- Treat CI as the final independent result.
- Store CI status and URL in workflow state.
- Keep pull requests in draft state.
- Do not merge automatically.

### Priority 2 — Simplify agent integration

#### Pluggable agent backends

Introduce a small common interface for planner, coder, and reviewer backends.

Supported backends may include:

- OpenAI API
- Azure OpenAI
- Open WebUI
- Codex CLI

Keep LangGraph state and workflow policy independent from a specific provider.

The interface should support:

- role-specific prompts
- bounded execution time
- structured output where required
- normalized errors
- optional repository write access

#### Codex-first coding workflow

Use Codex CLI as the preferred coding backend when quota is available:

```text
planner
→ Codex implementation
→ deterministic validation
→ Codex repair
→ deterministic validation
→ independent reviewer
```

The orchestrator remains responsible for issue and workspace lifecycle,
retries, checkpoints, validation, commits, push, and draft PR creation.

#### Prompt builder

Replace repeated string concatenation with role-specific prompt builders using:

- issue title and body
- repository context
- current step
- validation output
- review findings
- retry metadata
- repository instructions

#### Structured repository index

Evolve the current context collector into structured metadata:

- languages and frameworks
- build and test systems
- modules and package roots
- entry points
- important documentation
- validation commands

### Priority 3 — Add unattended queue execution

Add a sequential runner that:

- selects issues labeled `agent-ready`
- skips issues already running or represented by an open agent PR
- applies `agent-running`
- processes one issue at a time
- applies `agent-blocked` or `agent-completed`
- limits issues and failures per run
- preserves workspaces for blocked issues

Suggested labels:

```text
agent-ready
agent-running
agent-blocked
agent-completed
```

Only explicitly approved `agent-ready` issues may run unattended.

Target workflow:

```text
idea discussion
→ implementation plan
→ small GitHub issues
→ agent-ready queue
→ overnight implementation
→ validated draft PRs
→ morning review and merge
```

### Priority 4 — Operational improvements

- structured JSON logs per issue
- per-node and per-agent timings
- backend, token, cost, and quota reporting
- `--status`, `--restart`, and explicit resume targets
- checkpoint and workspace retention policies
- idempotent plan, review, blocked, and PR updates
- execution timeouts and nightly limits

### Later work

- dependency-aware execution plans
- optional parallel independent steps
- direct read-only repository exploration by planner and reviewer
- scheduled polling
- webhook startup
- optional parallel workers

## After MVP

### GitHub Actions validation

- Add a PR workflow that runs the full validation suite.
- Treat CI as the final independent result.
- Save CI status and URL in workflow state.
- Do not auto-merge in the first version.

### Blocked issue comments

- Post a concise issue comment with:
  - current step
  - retry count
  - blocking reason
  - resume command
- Avoid duplicate comments by using a stable marker.

### Better checkpoint controls

- Add `--status`.
- Add `--restart`.
- Add explicit resume targets.
- Add checkpoint cleanup for abandoned issues.

### Automation

Keep CLI as the default entry point first:

```bash
python -m app.graph --issue <number>
```

Then add, in order:

1. Polling for issues with an `agent:ready` label.
2. A single worker queue.
3. GitHub webhook startup.
4. Optional parallel workers.

### Operational improvements

- Structured JSON logging.
- Per-node execution timing.
- Token and cost reporting.
- Workspace retention policy.
- Duplicate plan and review comment prevention.
- Integration tests for graph routing.

---

## Recommended Delivery Order

1. Run and fix all MVP verification scenarios.
2. Add graph and resume integration tests.
3. Improve blocked-state reporting.
4. Add GitHub Actions validation.
5. Introduce pluggable agent backends.
6. Add prompt builders and structured repository metadata.
7. Add the sequential `agent-ready` queue.
8. Add structured logging and checkpoint controls.
9. Add scheduled polling.
10. Add webhooks.
11. Consider dependency-aware or parallel execution.

## MVP Completion Checklist

- [ ] Planner output is normalized into executable steps.
- [ ] Each step runs independently.
- [ ] Validation failures retry through the coder.
- [ ] Review failures retry through the coder.
- [ ] Retry limits are enforced.
- [ ] Each approved step is committed.
- [ ] Blocked workflows preserve workspace and checkpoint.
- [ ] `--resume` continues the same workflow.
- [ ] Final branch is pushed.
- [ ] Draft PR is created or updated.
- [ ] All six MVP verification scenarios pass.
- [x] Planner output is normalized into executable steps.

