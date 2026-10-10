from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.codex_cli import CodexCliError, run_structured_prompt


class PlanStep(BaseModel):
    id: str = Field(
        description="Stable step identifier, for example step-01."
    )
    title: str = Field(description="Short implementation step title.")
    goal: str = Field(description="Concrete outcome of this step.")
    requirements: list[str] = Field(
        description="Specific implementation requirements."
    )
    acceptance_criteria: list[str] = Field(
        description="Observable conditions proving this step is complete."
    )
    validation: list[str] = Field(
        description="Tests, checks, or commands expected to validate this step."
    )
    affected_areas: list[str] = Field(
        description="Likely repository modules, packages, or files affected."
    )
    out_of_scope: list[str] = Field(
        description="Explicit exclusions for this step."
    )
    depends_on: list[str] = Field(
        description="IDs of prerequisite steps."
    )


class ImplementationPlan(BaseModel):
    goal: str = Field(description="Concise overall goal of the issue.")
    summary: str = Field(
        description="Short technical summary of the proposed implementation."
    )
    assumptions: list[str] = Field(
        description="Assumptions made from the issue description."
    )
    open_questions: list[str] = Field(
        description=(
            "Questions requiring user input before implementation can "
            "proceed safely. "
            "Use an empty list when no clarification is required."
        )
    )
    acceptance_criteria: list[str] = Field(
        description="Overall issue-level acceptance criteria."
    )
    steps: list[PlanStep] = Field(
        min_length=1,
        description="Ordered, independently testable implementation steps.",
    )


class PlannerError(RuntimeError):
    pass


def consolidate_plan(plan: ImplementationPlan) -> ImplementationPlan:
    """Keep all plan requirements but make implementation one coding pass."""
    if len(plan.steps) <= 1:
        return plan
    step = PlanStep(
        id="implementation",
        title="Implement the complete task",
        goal=plan.goal,
        requirements=list(dict.fromkeys(
            requirement
            for item in plan.steps
            for requirement in item.requirements
        )),
        acceptance_criteria=list(dict.fromkeys(
            criterion
            for item in plan.steps
            for criterion in item.acceptance_criteria
        )),
        validation=list(dict.fromkeys(
            command
            for item in plan.steps
            for command in item.validation
        )),
        affected_areas=list(dict.fromkeys(
            area for item in plan.steps for area in item.affected_areas
        )),
        out_of_scope=list(dict.fromkeys(
            value for item in plan.steps for value in item.out_of_scope
        )),
        depends_on=[],
    )
    return plan.model_copy(update={
        "summary": (
            f"{plan.summary}\n\n"
            "The implementation plan is consolidated into one coding pass; "
            "all issue-level acceptance criteria remain in force."
        ),
        "steps": [step],
    })


def create_plan(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repository_context: str,
    workspace: Path,
) -> ImplementationPlan:
    task_reference = (
        f"task {abs(issue_number)}"
        if issue_number < 0
        else f"GitHub issue #{issue_number}"
    )
    prompt = f"""
You are the planning agent for the Investory repository.

Convert the GitHub issue into a small, ordered, and testable implementation
plan. Do not write code.

Task reference:
{task_reference}

Title:
{issue_title}

Body:
{issue_body or "No issue body was provided."}

Repository context:
{repository_context}

Planning rules:
- Answer repository-specific questions from the supplied repository context.
- Resolve routine technical decisions from repository conventions and prefer
  the smallest safe implementation. Do not ask about formatting or details
  discoverable from the repository.
- Add an open question only for a material product, security, or data-loss
  decision that cannot be safely inferred and blocks implementation.
- Separate product requirements from technical details.
- Do not invent behavior that is not supported by the issue.
- Record uncertain assumptions explicitly.
- Add open questions only when implementation would otherwise be unsafe or
  materially ambiguous.
- Prefer small steps that can be implemented and validated independently.
- Use a few coherent increments, each leaving formatting and relevant unit
  tests green. Avoid splitting trivial edits into separate steps. The
  orchestrator commits each validated increment and runs full validation and
  one independent whole-plan review after all steps are complete.
- Keep issue-required evidence gathering as a separate read-only step when it
  must happen before edits. State explicitly that the workspace must remain
  unchanged and identify the evidence to report.
- Every step must have concrete acceptance criteria.
- Treat the original prompt as the product contract. Turn its requested outcomes
  into a compact acceptance checklist; do not expand it with production-grade
  hardening, optional polish, or speculative edge cases.
- Map each acceptance criterion to the implementation area and its meaningful
  test/evidence in the step validation list. Group related boundary cases so
  one whole-plan audit can check them together.
- Every step must specify how it will be validated.
- List exact repository-relative files or directories in affected areas; do
  not use narrative labels as if they were filesystem paths. Use an empty list
  when affected paths cannot yet be named.
- Do not include branch creation, commits, pushes, or pull requests as steps.
- Do not include enhancements outside the issue scope.
- Order steps according to dependencies.
""".strip()

    try:
        result = run_structured_prompt(
            role="planner",
            prompt=prompt,
            response_model=ImplementationPlan,
            workspace=workspace,
            model=os.getenv("PLANNER_MODEL", ""),
        )
    except CodexCliError as error:
        raise PlannerError(
            f"Planner failed to create a structured plan: {error}"
        ) from error
    if not isinstance(result, ImplementationPlan):
        raise PlannerError("Planner returned an unexpected response type.")
    return result


def plan_to_markdown(plan: ImplementationPlan) -> str:
    lines = [
        "<!-- investory-orchestrator-plan -->",
        "## Automated implementation plan",
        "",
        f"**Goal:** {plan.goal}",
        "",
        plan.summary,
        "",
        "### Acceptance criteria",
        "",
    ]

    lines.extend(
        f"- [ ] {criterion}"
        for criterion in plan.acceptance_criteria
    )

    if plan.assumptions:
        lines.extend(["", "### Assumptions", ""])
        lines.extend(
            f"- {assumption}"
            for assumption in plan.assumptions
        )

    if plan.open_questions:
        lines.extend(["", "### Open questions", ""])
        lines.extend(
            f"- {question}"
            for question in plan.open_questions
        )

    lines.extend(["", "### Implementation steps", ""])

    for step in plan.steps:
        lines.extend(
            [
                f"#### {step.id}: {step.title}",
                "",
                step.goal,
                "",
                "**Requirements**",
                "",
            ]
        )

        lines.extend(
            f"- {requirement}"
            for requirement in step.requirements
        )

        lines.extend(["", "**Acceptance criteria**", ""])

        lines.extend(
            f"- [ ] {criterion}"
            for criterion in step.acceptance_criteria
        )

        if step.affected_areas:
            lines.extend(["", "**Affected areas**", ""])
            lines.extend(
                f"- `{area}`"
                for area in step.affected_areas
            )

        if step.depends_on:
            lines.extend(["", "**Dependencies**", ""])
            lines.extend(
                f"- `{dependency}`"
                for dependency in step.depends_on
            )

        if step.out_of_scope:
            lines.extend(["", "**Out of scope**", ""])
            lines.extend(
                f"- {item}"
                for item in step.out_of_scope
            )

        lines.extend(["", "**Validation**", ""])

        lines.extend(
            f"- `{validation}`"
            for validation in step.validation
        )

        lines.append("")

    lines.extend(
        [
            "---",
            "_Generated by Investory Orchestrator._",
        ]
    )

    return "\n".join(lines)
