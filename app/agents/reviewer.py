from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


ReviewStatus = Literal[
    "approved",
    "changes_required",
]


class ReviewFinding(BaseModel):
    severity: Literal[
        "blocking",
        "warning",
        "suggestion",
    ]
    title: str = Field(
        description="Short title describing the finding."
    )
    description: str = Field(
        description="Concrete explanation of the problem."
    )
    file: str | None = Field(
        default=None,
        description="Related repository file, when known.",
    )
    recommendation: str = Field(
        description="Specific recommended correction."
    )


class ReviewResult(BaseModel):
    status: ReviewStatus
    summary: str = Field(
        description="Concise review summary."
    )
    requirements_satisfied: list[str] = Field(
        description="Requirements that are demonstrably satisfied."
    )
    missing_requirements: list[str] = Field(
        description="Requirements not implemented or not demonstrated."
    )
    findings: list[ReviewFinding]
    tests_reviewed: list[str] = Field(
        description="Validation and test evidence considered."
    )


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


def _branch_diff(workspace: Path) -> str:
    committed = _run_git(
        workspace,
        ["diff", "--no-ext-diff", "origin/main...HEAD"],
    )

    uncommitted = _run_git(
        workspace,
        ["diff", "--no-ext-diff", "--", "."],
    )

    staged = _run_git(
        workspace,
        ["diff", "--cached", "--no-ext-diff", "--", "."],
    )

    sections: list[str] = []

    if committed.strip():
        sections.extend(
            [
                "## Committed branch diff",
                committed,
            ]
        )

    if staged.strip():
        sections.extend(
            [
                "## Staged diff",
                staged,
            ]
        )

    if uncommitted.strip():
        sections.extend(
            [
                "## Uncommitted diff",
                uncommitted,
            ]
        )

    if not sections:
        return "No changes compared with origin/main."

    return "\n\n".join(sections)


def review_implementation(
    *,
    workspace: Path,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan: dict,
    validation_output: str,
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

    diff = _branch_diff(workspace)

    prompt = f"""
You are reviewing an implementation in the Investory repository.

GitHub issue:

Number:
#{issue_number}

Title:
{issue_title}

Body:
{issue_body or "No issue body was provided."}

Approved implementation plan:
{plan}

Validation output:
{validation_output or "No validation output was supplied."}

Git diff:
{diff}

Review rules:
- Review only against the issue and approved implementation plan.
- Verify each overall and step-level acceptance criterion.
- Verify that validation evidence is relevant and successful.
- Check for missing behaviour, incorrect behaviour, unrelated changes,
  weakened tests, missing tests, unsafe error handling, and accidental
  implementation of future scope.
- Do not require stylistic changes unless they affect correctness,
  maintainability, or repository conventions.
- A blocking finding must result in status changes_required.
- Missing acceptance criteria must result in status changes_required.
- Warnings and suggestions alone may still result in approved.
- Do not modify code.
- Do not invent findings unsupported by the supplied diff or requirements.
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

    has_blocking_findings = any(
        finding.severity == "blocking"
        for finding in result.findings
    )

    if (
        has_blocking_findings
        or result.missing_requirements
    ):
        result.status = "changes_required"

    return result
