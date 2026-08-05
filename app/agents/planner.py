from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    id: str = Field(
        description="Stable step identifier, for example step-01."
    )
    title: str = Field(
        description="Short implementation step title."
    )
    goal: str = Field(
        description="Concrete outcome of this step."
    )
    requirements: list[str] = Field(
        description="Specific implementation requirements."
    )
    acceptance_criteria: list[str] = Field(
        description="Observable conditions proving this step is complete."
    )
    validation: list[str] = Field(
        description=(
            "Tests, checks, or commands expected to validate this step."
        )
    )
    affected_areas: list[str] = Field(
        description=(
            "Likely repository modules, packages, or files affected."
        )
    )
    out_of_scope: list[str] = Field(
        description="Explicit exclusions for this step."
    )
    depends_on: list[str] = Field(
        description="IDs of prerequisite steps."
    )


class ImplementationPlan(BaseModel):
    goal: str = Field(
        description="Concise overall goal of the issue."
    )
    summary: str = Field(
        description="Short technical summary of the proposed implementation."
    )
    assumptions: list[str] = Field(
        description="Assumptions made from the issue description."
    )
    open_questions: list[str] = Field(
        description=(
            "Questions requiring user input before safe implementation. "
            "Use an empty list when no clarification is required."
        )
    )
    acceptance_criteria: list[str] = Field(
        description="Overall issue-level acceptance criteria."
    )
    steps: list[PlanStep] = Field(
        min_length=1,
        description=(
            "Ordered, independently testable implementation steps."
        ),
    )


class PlannerError(RuntimeError):
    pass


def create_plan(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
) -> ImplementationPlan:
    model_name = os.getenv(
        "PLANNER_MODEL",
        "gpt-5.4-mini",
    )

    model = ChatOpenAI(
        model=model_name,
        temperature=0,
    )

    structured_model = model.with_structured_output(
        ImplementationPlan,
        method="json_schema",
    )

    prompt = f"""
You are the planning agent for the Investory repository.

Convert the GitHub issue into a small, ordered and testable implementation
plan. Do not write code.

GitHub issue:

Number:
#{issue_number}

Title:
{issue_title}

Body:
{issue_body or "No issue body was provided."}

Planning rules:
- Separate product requirements from technical implementation details.
- Do not invent product behaviour not supported by the issue.
- Record uncertain assumptions explicitly.
- Add open questions only when implementation would be unsafe or materially
  ambiguous without an answer.
- Prefer small steps that can be implemented and validated independently.
- Every step must have concrete acceptance criteria.
- Every step must describe expected validation.
- Do not include branch creation, commits, pushes, or pull request creation as
  implementation steps; the orchestrator handles those.
- Do not include future enhancements outside the issue scope.
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


def plan_to_markdown(
    plan: ImplementationPlan,
