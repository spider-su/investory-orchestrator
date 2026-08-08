from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from app.agents.reviewer import (
    ReviewFinding,
    ReviewResult,
    ReviewerError,
    _branch_diff,
    review_classification,
    review_identity,
    review_implementation,
    review_to_markdown,
)


class FakeStructuredModel:
    def __init__(self, result: object):
        self.result = result
        self.prompt = ""

    def invoke(self, prompt: str) -> object:
        self.prompt = prompt
        return self.result


class FakeChatOpenAI:
    last_instance: Optional["FakeChatOpenAI"] = None
    next_result: object = None

    def __init__(self, **kwargs: object):
        self.kwargs = kwargs
        self.schema: object = None
        self.method: Optional[str] = None
        self.structured_model: Optional[FakeStructuredModel] = None
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


def build_review(
    *,
    status: str = "approved",
    missing_requirements=None,
    findings=None,
) -> ReviewResult:
    return ReviewResult(
        status=status,
        summary="The implementation matches the plan.",
        requirements_satisfied=["Agent tests cover the happy path."],
        missing_requirements=missing_requirements or [],
        findings=findings or [],
        tests_reviewed=["python -m unittest discover -s app -p test_*.py"],
    )


class ReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path("D:/projects/investory-orchestrator")

    def test_branch_diff_collects_all_non_empty_sections(self) -> None:
        with patch(
            "app.agents.reviewer._run_git",
            side_effect=[
                "committed diff",
                "staged diff",
                "uncommitted diff",
                "",
            ],
        ):
            diff = _branch_diff(self.workspace)

        self.assertIn("## Committed branch diff", diff)
        self.assertIn("committed diff", diff)
        self.assertIn("## Staged diff", diff)
        self.assertIn("staged diff", diff)
        self.assertIn("## Uncommitted diff", diff)
        self.assertIn("uncommitted diff", diff)

    def test_review_identity_marks_same_or_missing_model_as_secondary(self) -> None:
        self.assertEqual(
            review_classification("", "gpt-5.4-mini"),
            "secondary_automated_review",
        )
        self.assertEqual(
            review_classification("gpt-5.4-mini", "gpt-5.4-mini"),
            "secondary_automated_review",
        )
        self.assertEqual(
            review_classification("codex-model", "review-model"),
            "independent",
        )

    def test_review_identity_uses_reviewer_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REVIEWER_MODEL": "review-model",
                "REVIEWER_PROVIDER": "review-provider",
            },
        ):
            self.assertEqual(
                review_identity(),
                {
                    "backend": "langchain-openai",
                    "provider": "review-provider",
                    "model": "review-model",
                },
            )

    def test_branch_diff_returns_default_message_without_changes(self) -> None:
        with patch(
            "app.agents.reviewer._run_git",
            side_effect=["  ", "", "\n", ""],
        ):
            diff = _branch_diff(self.workspace)

        self.assertEqual(diff, "No changes compared with origin/main.")

    def test_branch_diff_includes_untracked_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "new.py").write_text(
                "print('new')\n",
                encoding="utf-8",
            )

            with patch(
                "app.agents.reviewer._run_git",
                side_effect=["", "", "", "new.py"],
            ):
                diff = _branch_diff(workspace)

        self.assertIn("## Untracked file: new.py", diff)
        self.assertIn("print('new')", diff)

    def test_review_implementation_returns_structured_review(self) -> None:
        expected_review = build_review()

        with patch("app.agents.reviewer.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = expected_review

            with patch(
                "app.agents.reviewer._branch_diff",
                return_value="branch diff text",
            ):
                review = review_implementation(
                    workspace=self.workspace,
                    issue_number=42,
                    issue_title="Add tests for agents",
                    issue_body="Cover planner, coder, and reviewer.",
                    plan={"step": "step-01"},
                    validation_output="Validation succeeded.",
                )

        fake_model = FakeChatOpenAI.last_instance
        self.assertEqual(review, expected_review)
        self.assertEqual(fake_model.schema, ReviewResult)
        self.assertEqual(fake_model.method, "json_schema")
        self.assertEqual(fake_model.kwargs["model"], "gpt-5.4-mini")
        self.assertEqual(fake_model.kwargs["temperature"], 0)

        prompt = fake_model.structured_model.prompt
        self.assertIn("#42 — Add tests for agents", prompt)
        self.assertIn("Cover planner, coder, and reviewer.", prompt)
        self.assertIn("{'step': 'step-01'}", prompt)
        self.assertIn("Validation succeeded.", prompt)
        self.assertIn("branch diff text", prompt)

    def test_review_implementation_forces_changes_required_for_missing_requirements(self) -> None:
        with patch("app.agents.reviewer.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = build_review(
                status="approved",
                missing_requirements=["Reviewer should verify regression tests."],
            )

            with patch(
                "app.agents.reviewer._branch_diff",
                return_value="branch diff text",
            ):
                review = review_implementation(
                    workspace=self.workspace,
                    issue_number=1,
                    issue_title="Missing tests",
                    issue_body="",
                    plan={},
                    validation_output="",
                )

        self.assertEqual(review.status, "changes_required")

    def test_review_implementation_forces_changes_required_for_blocking_finding(self) -> None:
        with patch("app.agents.reviewer.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = build_review(
                status="approved",
                findings=[
                    ReviewFinding(
                        severity="blocking",
                        title="Missing coverage",
                        description="No tests were added for the new path.",
                        file="app/test_reviewer.py",
                        recommendation="Add reviewer coverage.",
                    )
                ],
            )

            with patch(
                "app.agents.reviewer._branch_diff",
                return_value="branch diff text",
            ):
                review = review_implementation(
                    workspace=self.workspace,
                    issue_number=1,
                    issue_title="Blocking finding",
                    issue_body="",
                    plan={},
                    validation_output="",
                )

        self.assertEqual(review.status, "changes_required")

    def test_review_implementation_wraps_model_errors(self) -> None:
        class ExplodingStructuredModel:
            def invoke(self, prompt: str) -> object:
                raise RuntimeError("boom")

        class ExplodingChatOpenAI:
            def __init__(self, **kwargs: object):
                self.kwargs = kwargs

            def with_structured_output(
                self,
                schema: object,
                *,
                method: str,
            ) -> ExplodingStructuredModel:
                return ExplodingStructuredModel()

        with patch("app.agents.reviewer.ChatOpenAI", ExplodingChatOpenAI):
            with patch(
                "app.agents.reviewer._branch_diff",
                return_value="branch diff text",
            ):
                with self.assertRaises(ReviewerError) as context:
                    review_implementation(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Reviewer failure",
                        issue_body="",
                        plan={},
                        validation_output="",
                    )

        self.assertIn(
            "Reviewer failed to produce a structured result: boom",
            str(context.exception),
        )

    def test_review_implementation_rejects_unexpected_response_type(self) -> None:
        with patch("app.agents.reviewer.ChatOpenAI", FakeChatOpenAI):
            FakeChatOpenAI.next_result = {"unexpected": True}

            with patch(
                "app.agents.reviewer._branch_diff",
                return_value="branch diff text",
            ):
                with self.assertRaises(ReviewerError) as context:
                    review_implementation(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Unexpected reviewer output",
                        issue_body="",
                        plan={},
                        validation_output="",
                    )

        self.assertIn(
            "Reviewer returned an unexpected response type.",
            str(context.exception),
        )

    def test_review_to_markdown_renders_findings_and_validation(self) -> None:
        review = build_review(
            status="changes_required",
            missing_requirements=["Add coverage for planner markdown output."],
            findings=[
                ReviewFinding(
                    severity="warning",
                    title="Narrow validation",
                    description="Only one agent path is exercised.",
                    file="app/test_planner.py",
                    recommendation="Add reviewer and coder tests.",
                )
            ],
        )

        markdown = review_to_markdown(review)

        self.assertIn("<!-- investory-orchestrator-review -->", markdown)
        self.assertIn("**Status:** Changes required", markdown)
        self.assertIn("### Requirements satisfied", markdown)
        self.assertIn("### Missing requirements", markdown)
        self.assertIn("### Findings", markdown)
        self.assertIn("**WARNING: Narrow validation**", markdown)
        self.assertIn("`app/test_planner.py`", markdown)
        self.assertIn("### Validation reviewed", markdown)
        self.assertIn(
            "_Generated by Investory Orchestrator._",
            markdown,
        )


if __name__ == "__main__":
    unittest.main()




