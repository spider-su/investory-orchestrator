from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import patch

from app.agents.planner import (
    ImplementationPlan,
    PlanStep,
    PlannerError,
    create_plan,
    plan_to_markdown,
)


def build_plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Add agent coverage",
        summary="Covers planner behavior with focused unit tests.",
        assumptions=["The issue text is complete."],
        open_questions=["Should review comments be published automatically?"],
        acceptance_criteria=["Planner returns a structured plan."],
        steps=[
            PlanStep(
                id="step-01",
                title="Add planner tests",
                goal="Verify structured plan generation.",
                requirements=["Mock the structured model invocation."],
                acceptance_criteria=["Planner errors are surfaced clearly."],
                validation=["python -m unittest app.test_planner"],
                affected_areas=["app/agents/planner.py"],
                out_of_scope=["Changing runtime planner behavior."],
                depends_on=[],
            ),
            PlanStep(
                id="step-02",
                title="Document markdown output",
                goal="Verify markdown includes optional sections.",
                requirements=["Render assumptions and dependencies."],
                acceptance_criteria=["Markdown includes checklist items."],
                validation=["python -m unittest app.test_planner"],
                affected_areas=["app/agents/planner.py"],
                out_of_scope=[],
                depends_on=["step-01"],
            ),
        ],
    )


class FakeStructuredModel:
    def __init__(self, result: object) -> None:
        self.result = result
        self.prompt = ""

    def invoke(self, prompt: str) -> object:
        self.prompt = prompt
        return self.result


class FakeChatOpenAI:
    last_instance: Optional["FakeChatOpenAI"] = None
    next_result: object = None

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.structured_model: Optional[FakeStructuredModel] = None
        self.schema: object = None
        self.method: Optional[str] = None
        FakeChatOpenAI.last_instance = self

    def with_structured_output(
        self,
        schema: object,
        *,
        method: str,
    ) -> FakeStructuredModel:
        self.schema = schema
        self.method = method
        self.structured_model = FakeStructuredModel(
            FakeChatOpenAI.next_result
        )
        return self.structured_model


class PlannerTests(unittest.TestCase):
    def test_create_plan_returns_structured_plan(self) -> None:
        expected_plan = build_plan()

        with patch("app.agents.planner.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = expected_plan

            plan = create_plan(
                issue_number=27,
                issue_title="Add agent tests",
                issue_body="Cover planner, coder, and reviewer.",
            )

        fake_model = FakeChatOpenAI.last_instance
        self.assertEqual(plan, expected_plan)
        self.assertEqual(fake_model.schema, ImplementationPlan)
        self.assertEqual(fake_model.method, "json_schema")
        self.assertEqual(fake_model.kwargs["model"], "gpt-5.4-mini")
        self.assertEqual(fake_model.kwargs["temperature"], 0)
        self.assertIn("#27", fake_model.structured_model.prompt)
        self.assertIn("Add agent tests", fake_model.structured_model.prompt)
        self.assertIn(
            "Cover planner, coder, and reviewer.",
            fake_model.structured_model.prompt,
        )

    def test_create_plan_uses_default_body_text_when_issue_body_missing(self) -> None:
        expected_plan = build_plan()

        with patch("app.agents.planner.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = expected_plan

            create_plan(
                issue_number=11,
                issue_title="Handle empty issue body",
                issue_body="",
            )

        fake_model = FakeChatOpenAI.last_instance
        self.assertIn(
            "No issue body was provided.",
            fake_model.structured_model.prompt,
        )

    def test_create_plan_wraps_model_errors(self) -> None:
        class ExplodingStructuredModel:
            def invoke(self, prompt: str) -> object:
                raise RuntimeError("boom")

        class ExplodingChatOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def with_structured_output(
                self,
                schema: object,
                *,
                method: str,
            ) -> ExplodingStructuredModel:
                return ExplodingStructuredModel()

        with patch("app.agents.planner.ChatOpenAI", ExplodingChatOpenAI):
            with self.assertRaises(PlannerError) as context:
                create_plan(
                    issue_number=5,
                    issue_title="Planner failure",
                    issue_body="",
                )

        self.assertIn(
            "Planner failed to create a structured plan: boom",
            str(context.exception),
        )

    def test_create_plan_rejects_unexpected_response_type(self) -> None:
        with patch("app.agents.planner.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = {"unexpected": True}

            with self.assertRaises(PlannerError) as context:
                create_plan(
                    issue_number=5,
                    issue_title="Unexpected planner output",
                    issue_body="",
                )

        self.assertIn(
            "Planner returned an unexpected response type.",
            str(context.exception),
        )

    def test_plan_to_markdown_renders_optional_sections(self) -> None:
        markdown = plan_to_markdown(build_plan())

        self.assertIn("<!-- investory-orchestrator-plan -->", markdown)
        self.assertIn("## Automated implementation plan", markdown)
        self.assertIn("### Assumptions", markdown)
        self.assertIn("### Open questions", markdown)
        self.assertIn("#### step-01: Add planner tests", markdown)
        self.assertIn("**Affected areas**", markdown)
        self.assertIn("**Dependencies**", markdown)
        self.assertIn("`step-01`", markdown)
        self.assertIn("- [ ] Planner returns a structured plan.", markdown)
        self.assertIn(
            "_Generated by Investory Orchestrator._",
            markdown,
        )


if __name__ == "__main__":
    unittest.main()


