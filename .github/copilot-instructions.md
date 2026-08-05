# GitHub Copilot Instructions

Use `AGENTS.md` as the canonical project guide.

## Copilot-Specific Overlay

- Generate Python that matches the repository's existing style: explicit control flow,
  descriptive names, type hints where they improve clarity, and small focused functions.
- Inspect `app/graph.py`, `app/state.py`, and the relevant agent or integration module before
  changing workflow behavior. Preserve existing LangGraph node, route, and state contracts.
- Treat `WorkflowState` as the source of truth for persisted workflow data. When adding state,
  initialize it in the CLI entry point and preserve it across retries and resume operations.
- Keep graph changes internally consistent: every routed label must map to an existing node,
  every referenced node must be registered, and terminal paths must reach cleanup or `END`.
- Preserve the separation of responsibilities:
    - planner creates structured plans;
    - coder edits the target repository;
    - validator executes validation without editing code;
    - reviewer evaluates the implementation;
    - orchestrator coordinates state and routing.
- Do not let agents commit, push, or create pull requests directly. Those operations belong in
  orchestrator workspace and GitHub integration nodes.
- Prefer existing helpers in `app/workspace.py`, `app/github_client.py`, and
  `app/test_runner.py` before introducing new subprocess or GitHub API logic.
- Keep GitHub operations idempotent where practical. Reuse an existing issue branch or open pull
  request instead of creating duplicates.
- Preserve checkpoint compatibility. Use the stable thread ID
  `investory-issue-<issue-number>` and avoid silently restarting a resumable workflow.
- Do not guess LangGraph, PyGithub, OpenAI, Docker, or Dev Container APIs. Use the repository's
  dependency declarations and existing code as the source of truth.
- Never print installation tokens, private keys, authorization headers, environment secrets, or
  full credential-bearing commands.
- Use `python -m py_compile` for a fast syntax check after Python changes, then run the
  repository's containerized validation path when workflow behavior is affected.
- Do not duplicate project architecture, workflow policy, or agent responsibilities here.
  Update `AGENTS.md` when those canonical rules change.
