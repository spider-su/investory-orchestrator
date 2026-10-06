from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.codex_cli import CodexCliError
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


def build_review(*, status="approved", missing_requirements=None, findings=None):
    return ReviewResult(
        status=status,
        summary="The implementation matches the plan.",
        requirements_satisfied=["Agent tests cover the happy path."],
        missing_requirements=missing_requirements or [],
        findings=findings or [],
        tests_reviewed=["python -m unittest discover -s tests"],
    )


class ReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_branch_diff_collects_all_non_empty_sections(self) -> None:
        with patch(
            "app.agents.reviewer._run_git",
            side_effect=["committed diff", "staged diff", "uncommitted diff", ""],
        ):
            diff = _branch_diff(self.workspace)

        self.assertIn("## Committed branch diff", diff)
        self.assertIn("## Staged diff", diff)
        self.assertIn("## Uncommitted diff", diff)

    def test_branch_diff_returns_default_message_without_changes(self) -> None:
        with patch(
            "app.agents.reviewer._run_git",
            side_effect=["  ", "", "\n", ""],
        ):
            diff = _branch_diff(self.workspace)

        self.assertEqual(diff, "No changes compared with origin/main.")

    def test_branch_diff_includes_untracked_file_contents(self) -> None:
        (self.workspace / "new.py").write_text(
            "print('new')\n",
            encoding="utf-8",
        )
        with patch(
            "app.agents.reviewer._run_git",
            side_effect=["", "", "", "new.py"],
        ):
            diff = _branch_diff(self.workspace)

        self.assertIn("## Untracked file: new.py", diff)
        self.assertIn("print('new')", diff)

    def test_review_identity_defaults_to_local_codex(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                review_identity(),
                {"backend": "codex-cli", "provider": "codex-cli", "model": ""},
            )

    def test_review_identity_uses_reviewer_model_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {"REVIEWER_MODEL": "review-model"},
        ):
            self.assertEqual(
                review_identity(),
                {"backend": "codex-cli", "provider": "codex-cli", "model": "review-model"},
            )

    def test_review_classification_requires_different_known_models(self) -> None:
        self.assertEqual(
            review_classification("", "review-model", coder_provider="codex-cli", reviewer_provider="codex-cli"),
            "secondary_automated_review",
        )
        self.assertEqual(
            review_classification("same", "same", coder_provider="codex-cli", reviewer_provider="codex-cli"),
            "secondary_automated_review",
        )
        self.assertEqual(
            review_classification("coder", "reviewer", coder_provider="codex-cli", reviewer_provider="codex-cli"),
            "independent",
        )

    def _run_review(self, result, *, review_scope="step"):
        with patch(
            "app.agents.reviewer.run_structured_prompt",
            return_value=result,
        ) as run_mock:
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
                    review_scope=review_scope,
                )
        return review, run_mock

    def test_review_implementation_uses_read_only_local_codex(self) -> None:
        expected = build_review()
        with patch.dict("os.environ", {"REVIEWER_MODEL": "review-model"}):
            review, run_mock = self._run_review(expected)

        self.assertEqual(review, expected)
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["role"], "reviewer")
        self.assertIs(kwargs["response_model"], ReviewResult)
        self.assertEqual(kwargs["workspace"], self.workspace)
        self.assertEqual(kwargs["model"], "review-model")
        self.assertIn("#42 — Add tests for agents", kwargs["prompt"])
        self.assertIn("Validation succeeded.", kwargs["prompt"])
        self.assertIn("branch diff text", kwargs["prompt"])
        self.assertIn("Do not report", kwargs["prompt"])
        self.assertIn("later planned steps as context", kwargs["prompt"])

    def test_review_receives_orchestrator_evidence_and_coder_report(self) -> None:
        with patch(
            "app.agents.reviewer.run_structured_prompt",
            return_value=build_review(),
        ) as run_mock:
            with patch("app.agents.reviewer._branch_diff", return_value="diff"):
                review_implementation(
                    workspace=self.workspace,
                    issue_number=104,
                    issue_title="Documentation",
                    issue_body="Update docs.",
                    plan={"current_step": {"id": "step-01"}},
                    validation_output="Validation passed.",
                    baseline_sha="step-baseline",
                    coder_report={"summary": "Inventory complete."},
                    workspace_audit={
                        "branch": "agent/issue-104",
                        "clean": True,
                    },
                )

        prompt = run_mock.call_args.kwargs["prompt"]
        self.assertIn("Inventory complete.", prompt)
        self.assertIn('"clean": true', prompt)
        self.assertIn("Do not demand historical evidence", prompt)

    def test_whole_plan_review_covers_all_steps(self) -> None:
        _, run_mock = self._run_review(
            build_review(),
            review_scope="whole_plan",
        )

        prompt = run_mock.call_args.kwargs["prompt"]
        self.assertIn("Review the complete implementation across every plan step.", prompt)
        self.assertIn("assess the issue and", prompt)

    def test_review_forces_changes_required_for_missing_requirements(self) -> None:
        review, _ = self._run_review(
            build_review(missing_requirements=["Add regression tests."])
        )
        self.assertEqual(review.status, "changes_required")

    def test_review_forces_changes_required_for_blocking_finding(self) -> None:
        review, _ = self._run_review(
            build_review(
                findings=[ReviewFinding(
                    severity="blocking",
                    title="Missing coverage",
                    description="No regression test covers this path.",
                    file="app/test_reviewer.py",
                    recommendation="Add a focused test.",
                )]
            )
        )
        self.assertEqual(review.status, "changes_required")

    def test_review_wraps_codex_errors(self) -> None:
        with patch(
            "app.agents.reviewer.run_structured_prompt",
            side_effect=CodexCliError("CLI unavailable"),
        ):
            with patch("app.agents.reviewer._branch_diff", return_value="diff"):
                with self.assertRaisesRegex(
                    ReviewerError,
                    "Reviewer failed to produce a structured result: CLI unavailable",
                ):
                    review_implementation(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Reviewer failure",
                        issue_body="",
                        plan={},
                        validation_output="",
                    )

    def test_review_rejects_unexpected_response_type(self) -> None:
        with patch(
            "app.agents.reviewer.run_structured_prompt",
            return_value={"unexpected": True},
        ):
            with patch("app.agents.reviewer._branch_diff", return_value="diff"):
                with self.assertRaisesRegex(
                    ReviewerError,
                    "Reviewer returned an unexpected response type",
                ):
                    review_implementation(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Invalid reviewer result",
                        issue_body="",
                        plan={},
                        validation_output="",
                    )

    def test_review_to_markdown_renders_findings_and_validation(self) -> None:
        review = build_review(
            status="changes_required",
            missing_requirements=["Add regression coverage."],
            findings=[ReviewFinding(
                severity="warning",
                title="Narrow validation",
                description="Only one role is exercised.",
                file="tests/test_reviewer.py",
                recommendation="Cover all agent roles.",
            )],
        )
        markdown = review_to_markdown(review)

        self.assertIn("<!-- investory-orchestrator-review -->", markdown)
        self.assertIn("**Status:** Changes required", markdown)
        self.assertIn("### Requirements satisfied", markdown)
        self.assertIn("### Missing requirements", markdown)
        self.assertIn("**WARNING: Narrow validation**", markdown)
        self.assertIn("### Validation reviewed", markdown)


if __name__ == "__main__":
    unittest.main()
