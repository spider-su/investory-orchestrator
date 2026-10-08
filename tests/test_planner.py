from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.codex_cli import CodexCliError
from app.agents.planner import (
    ImplementationPlan,
    PlanStep,
    PlannerError,
    create_plan,
    consolidate_plan,
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
                requirements=["Mock the Codex structured invocation."],
                acceptance_criteria=["Planner errors are surfaced clearly."],
                validation=["python -m unittest tests.test_planner"],
                affected_areas=["app/agents/planner.py"],
                out_of_scope=["Changing runtime planner behavior."],
                depends_on=[],
            )
        ],
    )


class PlannerTests(unittest.TestCase):
    def test_create_plan_uses_local_codex_and_returns_structured_plan(self) -> None:
        expected = build_plan()
        with patch.dict("os.environ", {"PLANNER_MODEL": "codex-plan-model"}):
            with patch(
                "app.agents.planner.run_structured_prompt",
                return_value=expected,
            ) as run_mock:
                plan = create_plan(
                    issue_number=27,
                    issue_title="Add agent tests",
                    issue_body="Cover planner, coder, and reviewer.",
                    repository_context="Repository context.",
                    workspace=Path("/tmp/worktree"),
                )

        self.assertEqual(plan, expected)
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["role"], "planner")
        self.assertIs(kwargs["response_model"], ImplementationPlan)
        self.assertEqual(kwargs["workspace"], Path("/tmp/worktree"))
        self.assertEqual(kwargs["model"], "codex-plan-model")
        self.assertIn("#27", kwargs["prompt"])
        self.assertIn("Add agent tests", kwargs["prompt"])
        self.assertIn("Cover planner, coder, and reviewer.", kwargs["prompt"])

    def test_create_plan_handles_missing_issue_body(self) -> None:
        with patch(
            "app.agents.planner.run_structured_prompt",
            return_value=build_plan(),
        ) as run_mock:
            create_plan(
                issue_number=11,
                issue_title="Handle empty issue body",
                issue_body="",
                repository_context="Repository context.",
                workspace=Path("/tmp/worktree"),
            )

        self.assertIn(
            "No issue body was provided.",
            run_mock.call_args.kwargs["prompt"],
        )

    def test_create_plan_wraps_codex_errors(self) -> None:
        with patch(
            "app.agents.planner.run_structured_prompt",
            side_effect=CodexCliError("CLI unavailable"),
        ):
            with self.assertRaisesRegex(
                PlannerError,
                "Planner failed to create a structured plan: CLI unavailable",
            ):
                create_plan(
                    issue_number=5,
                    issue_title="Planner failure",
                    issue_body="",
                    repository_context="Repository context.",
                    workspace=Path("/tmp/worktree"),
                )

    def test_create_plan_rejects_unexpected_response_type(self) -> None:
        with patch(
            "app.agents.planner.run_structured_prompt",
            return_value={"unexpected": True},
        ):
            with self.assertRaisesRegex(
                PlannerError,
                "Planner returned an unexpected response type",
            ):
                create_plan(
                    issue_number=5,
                    issue_title="Unexpected planner output",
                    issue_body="",
                    repository_context="Repository context.",
                    workspace=Path("/tmp/worktree"),
                )

    def test_plan_to_markdown_renders_optional_sections(self) -> None:
        markdown = plan_to_markdown(build_plan())

        self.assertIn("<!-- investory-orchestrator-plan -->", markdown)
        self.assertIn("## Automated implementation plan", markdown)
        self.assertIn("### Assumptions", markdown)
        self.assertIn("### Open questions", markdown)
        self.assertIn("#### step-01: Add planner tests", markdown)
        self.assertIn("**Affected areas**", markdown)
        self.assertIn("- [ ] Planner returns a structured plan.", markdown)
        self.assertIn("_Generated by Investory Orchestrator._", markdown)

    def test_consolidate_plan_keeps_all_criteria_in_one_step(self) -> None:
        plan = build_plan()
        second = plan.steps[0].model_copy(update={
            "id": "step-02",
            "title": "Cover API behavior",
            "requirements": ["Add endpoint coverage."],
            "acceptance_criteria": ["Read-only route is covered."],
            "validation": ["python -m unittest tests.test_planner"],
            "affected_areas": ["app/agents/planner.py", "tests/"],
            "depends_on": ["step-01"],
        })
        plan = plan.model_copy(update={"steps": [plan.steps[0], second]})

        result = consolidate_plan(plan)

        self.assertEqual(len(result.steps), 1)
        step = result.steps[0]
        self.assertEqual(step.id, "implementation")
        self.assertEqual(step.requirements, [
            "Mock the Codex structured invocation.", "Add endpoint coverage."
        ])
        self.assertEqual(step.acceptance_criteria, [
            "Planner errors are surfaced clearly.", "Read-only route is covered."
        ])
        self.assertEqual(step.validation, ["python -m unittest tests.test_planner"])
        self.assertEqual(step.affected_areas, ["app/agents/planner.py", "tests/"])
        self.assertIn("one coding pass", result.summary)


if __name__ == "__main__":
    unittest.main()
