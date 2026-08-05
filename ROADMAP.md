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
- Otherwise create a draft PR to `main`.
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

1. Multi-step execution
2. Retry loop
3. Step commits
4. Resume support
5. Draft PR
6. End-to-end MVP scenarios
7. GitHub Actions
8. Blocked comments
9. Polling
10. Webhooks

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
