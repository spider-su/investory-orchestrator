from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


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


def create_plan(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repository_context: str,
) -> ImplementationPlan:
    model = ChatOpenAI(
        model=os.getenv("PLANNER_MODEL", "gpt-5.4-mini"),
        temperature=0,
    )

    structured_model = model.with_structured_output(
        ImplementationPlan,
        method="json_schema",
    )

    prompt = f"""
You are the planning agent for the Investory repository.

Convert the GitHub issue into a small, ordered, and testable implementation
plan. Do not write code.

GitHub issue number:
#{issue_number}

Title:
{issue_title}

Body:
{issue_body or "No issue body was provided."}

Repository context:
{repository_context}

Planning rules:
- Answer repository-specific questions from the supplied repository context.
- Ask the user only about product decisions that cannot be inferred safely.
- Separate product requirements from technical details.
- Do not invent behavior that is not supported by the issue.
- Record uncertain assumptions explicitly.
- Add open questions only when implementation would otherwise be unsafe or
  materially ambiguous.
- Prefer small steps that can be implemented and validated independently.
- Every step must have concrete acceptance criteria.
- Every step must specify how it will be validated.
- Do not include branch creation, commits, pushes, or pull requests as steps.
- Do not include enhancements outside the issue scope.
- Order steps according to dependencies.
""".strip()

    try:
        result = structured_model.invoke(prompt)
    except Exception as error:
        raise PlannerError(
            f"Planner failed to create a structured plan: {error}"
        ) from error

    if not isinstance(result, ImplementationPlan):
        raise PlannerError(
            "Planner returned an unexpected response type."
        )

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