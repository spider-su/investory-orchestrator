from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.codex_cli import CodexCliError, run_structured_prompt


ReviewStatus = Literal["approved", "changes_required"]
ReviewScope = Literal["step", "whole_plan"]


class ReviewFinding(BaseModel):
    severity: Literal["blocking", "warning", "suggestion"]
    title: str
    description: str
    file: str | None = None
    recommendation: str


class ReviewResult(BaseModel):
    status: ReviewStatus
    summary: str
    requirements_satisfied: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    tests_reviewed: list[str] = Field(default_factory=list)


class ReviewerError(RuntimeError):
    pass


def review_model() -> str:
    return os.getenv("REVIEWER_MODEL", "")


def review_identity() -> dict[str, str]:
    return {
        "backend": "codex-cli",
        "provider": "codex-cli",
        "model": review_model(),
    }


def review_classification(
    coder_model: str,
    reviewer_model: str,
    *,
    coder_provider: str = "",
    reviewer_provider: str = "",
) -> str:
    known_coder_identity = bool(coder_model and coder_provider)
    known_reviewer_identity = bool(reviewer_model and reviewer_provider)
    identities_differ = (
        (coder_provider, coder_model)
        != (reviewer_provider, reviewer_model)
    )

    if (
        known_coder_identity
        and known_reviewer_identity
        and identities_differ
        and coder_provider != "unknown"
        and reviewer_provider != "unknown"
    ):
        return "independent"

    return "secondary_automated_review"


def _run_git(
    workspace: Path,
    command: list[str],
) -> str:
    result = subprocess.run(
        ["git", *command],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        raise ReviewerError(
            f"Git command failed: git {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    return result.stdout


def _untracked_diff(workspace: Path, *, limit: int = 20_000) -> str:
    names = _run_git(
        workspace,
        ["ls-files", "--others", "--exclude-standard", "--", "."],
    ).splitlines()
    sections: list[str] = []

    workspace_root = workspace.resolve()

    for name in names:
        relative_path = Path(name)
        path = (workspace / relative_path).resolve()

        if relative_path.is_absolute() or not path.is_relative_to(workspace_root):
            continue

        try:
            content = path.read_bytes()
        except OSError as error:
            rendered = f"<unable to read untracked file: {error}>"
        else:
            if b"\x00" in content:
                rendered = f"<binary file, {len(content)} bytes>"
            else:
                rendered = content.decode("utf-8", errors="replace")
                if len(rendered) > limit:
                    rendered = rendered[:limit] + "\n... <truncated>"

        sections.extend(
            [
                f"## Untracked file: {relative_path.as_posix()}",
                rendered,
            ]
        )

    return "\n\n".join(sections)


def _branch_diff(
    workspace: Path,
    *,
    baseline_sha: str | None = None,
) -> str:
    sections: list[str] = []
    committed_range = (
        f"{baseline_sha}..HEAD"
        if baseline_sha
        else "origin/main...HEAD"
    )

    committed = _run_git(
        workspace,
        ["diff", "--no-ext-diff", committed_range],
    )
    if committed.strip():
        sections.extend(
            [
                "## Committed branch diff",
                committed,
            ]
        )

    staged = _run_git(
        workspace,
        ["diff", "--cached", "--no-ext-diff", "--", "."],
    )
    if staged.strip():
        sections.extend(
            [
                "## Staged diff",
                staged,
            ]
        )

    uncommitted = _run_git(
        workspace,
        ["diff", "--no-ext-diff", "--", "."],
    )
    if uncommitted.strip():
        sections.extend(
            [
                "## Uncommitted diff",
                uncommitted,
            ]
        )

    untracked = _untracked_diff(workspace)
    if untracked.strip():
        sections.append(untracked)

    return (
        "\n\n".join(sections)
        if sections
        else (
            "No changes compared with "
            f"{baseline_sha or 'origin/main'}."
        )
    )


def review_implementation(
    *,
    workspace: Path,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan: dict,
    validation_output: str,
    review_scope: ReviewScope = "step",
    baseline_sha: str | None = None,
    coder_report: dict | None = None,
    workspace_audit: dict | None = None,
) -> ReviewResult:
    scope_rules = (
        """
- Review the complete implementation across every plan step.
- Verify all issue-level acceptance criteria and interactions between steps.
- Do not request redesign solely for maintainability, style, or preference.
- Request changes to an earlier checkpoint only when a critical defect or an
  unmet explicit plan requirement makes that necessary.
"""
        if review_scope == "whole_plan"
        else """
- Evaluate the current step's goal, requirements, acceptance criteria,
  validation, and affected areas.
- Treat issue-level criteria and later planned steps as context. Do not report
  work assigned to a later step as missing before that step is implemented.
- Report direct regressions caused by the current candidate even when they
  affect behavior from an earlier step.
"""
    ).strip()

    task_reference = (
        f"task {abs(issue_number)}"
        if issue_number < 0
        else f"GitHub issue #{issue_number}"
    )
    prompt = f"""
You are reviewing an implementation in the Investory repository.

Task:
{task_reference} — {issue_title}

Issue body:
{issue_body or "No issue body was provided."}

Approved implementation plan:
{plan}

Coder report (agent-authored, corroborate it against the diff and validation):
{json.dumps(coder_report or {}, indent=2, sort_keys=True)}

Orchestrator-captured initial workspace audit (captured before planning or
agent edits; authoritative for the original branch, commit, and initial
tracked/untracked status):
{json.dumps(workspace_audit or {}, indent=2, sort_keys=True)}

Validation output:
{validation_output or "No validation output was supplied."}

Git diff:
{_branch_diff(workspace, baseline_sha=baseline_sha)}

Review scope:
{review_scope}

Review rules:
{scope_rules}
- The review gate has two responsibilities:
  1. Confirm the supplied validation evidence is successful. Workflow routing
     sends code to review only after the configured validation gate passes;
     GitHub Actions is checked separately after the draft PR is created.
  2. Confirm every explicit requirement and acceptance criterion in the active
     review scope is implemented. At whole-plan review, verify every planned
     step and issue-level acceptance criterion; do not accept work deferred to
     a future task or issue.
- Review only against the issue and approved plan.
- Review only the active implementation step. Work assigned to later steps
  is out of scope until those steps are active.
- Use the orchestrator-captured workspace audit to verify initial checkout
  status and preservation claims. Do not demand historical evidence that the
  orchestrator captured directly before any agent ran.
- Verify acceptance criteria for the active review scope. For a step review,
  assess the current step only; for a whole-plan review, assess the issue and
  all plan steps.
- Only report a requirement as missing when it is explicit in the issue or
  approved plan and is not implemented or already satisfied with evidence.
- Use `blocking` only for a critical defect that makes implemented behavior
  materially incorrect, unsafe, or violates an explicit acceptance criterion.
  Missing explicit plan work is also a gate failure. Do not mark medium or minor
  concerns as blocking.
- Use `warning` or `suggestion` for non-critical improvements, including
  optional test expansion, maintainability, style, or follow-up work. These are
  recorded in the review comment and must not send the coder back.
- The final status is `changes_required` only when explicit requirements are
  missing or at least one critical (`blocking`) finding exists. Otherwise it
  is `approved`, even when warnings or suggestions are present.
- Do not modify code.
- Do not invent findings unsupported by the supplied evidence.
""".strip()

    try:
        result = run_structured_prompt(
            role="reviewer",
            prompt=prompt,
            response_model=ReviewResult,
            workspace=workspace,
            model=review_model(),
        )
    except CodexCliError as error:
        raise ReviewerError(
            f"Reviewer failed to produce a structured result: {error}"
        ) from error
    if not isinstance(result, ReviewResult):
        raise ReviewerError("Reviewer returned an unexpected response type.")

    has_gate_failure = bool(result.missing_requirements) or any(
        finding.severity == "blocking"
        for finding in result.findings
    )
    result.status = "changes_required" if has_gate_failure else "approved"

    return result


def review_to_markdown(review: ReviewResult) -> str:
    status_text = (
        "Approved"
        if review.status == "approved"
        else "Changes required"
    )

    lines = [
        "<!-- investory-orchestrator-review -->",
        "## Automated implementation review",
        "",
        f"**Status:** {status_text}",
        "",
        review.summary,
    ]

    if review.requirements_satisfied:
        lines.extend(
            [
                "",
                "### Requirements satisfied",
                "",
            ]
        )
        lines.extend(
            f"- {item}"
            for item in review.requirements_satisfied
        )

    if review.missing_requirements:
        lines.extend(
            [
                "",
                "### Missing requirements",
                "",
            ]
        )
        lines.extend(
            f"- [ ] {item}"
            for item in review.missing_requirements
        )

    if review.findings:
        lines.extend(["", "### Findings", ""])

        for finding in review.findings:
            file_suffix = (
                f" — `{finding.file}`"
                if finding.file
                else ""
            )

            lines.extend(
                [
                    (
                        f"- **{finding.severity.upper()}: "
                        f"{finding.title}**{file_suffix}"
                    ),
                    f"  - {finding.description}",
                    f"  - Fix: {finding.recommendation}",
                ]
            )

    if review.tests_reviewed:
        lines.extend(
            [
                "",
                "### Validation reviewed",
                "",
            ]
        )
        lines.extend(
            f"- {item}"
            for item in review.tests_reviewed
        )

    lines.extend(
        [
            "",
            "---",
            "_Generated by Investory Orchestrator._",
        ]
    )

    return "\n".join(lines)
