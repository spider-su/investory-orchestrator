from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


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
) -> ReviewResult:
    model = ChatOpenAI(
        model=os.getenv(
            "REVIEWER_MODEL",
            os.getenv("PLANNER_MODEL", "gpt-5.4-mini"),
        ),
        temperature=0,
    )

    structured_model = model.with_structured_output(
        ReviewResult,
        method="json_schema",
    )

    scope_rules = (
        """
- Review the complete implementation across every plan step.
- Verify all issue-level acceptance criteria and interactions between steps.
- Look for abstractions that became unsuitable as later steps were added.
- Look for duplicated concepts, compensating workarounds, inconsistent public
  APIs, incompatible migrations or configuration, and missing integration tests.
- A locally valid earlier step must not be treated as immutable.
- Request changes when redesigning an earlier checkpoint would produce a more
  coherent implementation.
"""
        if review_scope == "whole_plan"
        else """
- Concentrate on the current implementation step and its acceptance criteria.
- Treat earlier approved steps as context, while still reporting direct
  regressions caused by the current candidate.
"""
    ).strip()

    prompt = f"""
You are reviewing an implementation in the Investory repository.

Issue:
#{issue_number} — {issue_title}

Issue body:
{issue_body or "No issue body was provided."}

Approved implementation plan:
{plan}

Validation output:
{validation_output or "No validation output was supplied."}

Git diff:
{_branch_diff(workspace, baseline_sha=baseline_sha)}

Review scope:
{review_scope}

Review rules:
{scope_rules}
- Review only against the issue and approved plan.
- Verify overall and step-level acceptance criteria.
- Check for missing behaviour, incorrect behaviour, unrelated changes,
  weakened tests, missing tests, and unsafe error handling.
- Any unmet acceptance criterion requires the changes_required status.
- Any blocking finding requires the changes_required status.
- An implementation may still be approved if it has only warnings or
  suggestions.
- Do not modify code.
- Do not invent findings unsupported by the supplied evidence.
""".strip()

    try:
        result = structured_model.invoke(prompt)
    except Exception as error:
        raise ReviewerError(
            f"Reviewer failed to produce a structured result: {error}"
        ) from error

    if not isinstance(result, ReviewResult):
        raise ReviewerError(
            "Reviewer returned an unexpected response type."
        )

    if result.missing_requirements or any(
        finding.severity == "blocking"
        for finding in result.findings
    ):
        result.status = "changes_required"

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