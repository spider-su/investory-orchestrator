from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.planner import ImplementationPlan, PlanStep
from app.agents.reviewer import ReviewResult
from app.graph import build_graph, close_graph


class FakePullRequest:
    def __init__(self) -> None:
        self.number = 7
        self.html_url = "https://example.test/pull/7"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.created_pull_requests: list[dict[str, str]] = []
        self.branch_heads: dict[str, str] = {}

    def get_issue(self, issue_number: int):
        return SimpleNamespace(
            number=issue_number,
            title="Add a deterministic fixture",
            body="Implement one small, fully specified change.",
        )

    def upsert_issue_comment(
        self,
        issue_number: int,
        body: str,
        *,
        marker: str,
    ) -> int:
        self.comments.append(body)
        return len(self.comments)

    def get_branch_head_sha(self, branch: str) -> str | None:
        return self.branch_heads.get(branch)

    def find_open_pr_by_branch(self, branch: str):
        return None

    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> FakePullRequest:
        self.created_pull_requests.append(
            {
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            }
        )
        return FakePullRequest()


def initial_state(issue_number: int) -> dict:
    return {
        "issue_number": issue_number,
        "issue_title": "",
        "issue_body": "",
        "repository_context": "",
        "workflow_status": "new",
        "plan": {},
        "plan_markdown": "",
        "plan_published": False,
        "planning_error": "",
        "requires_user_input": False,
        "steps": [],
        "current_step": 0,
        "completed_steps": [],
        "workspace": "",
        "branch": "",
        "issue_baseline_sha": "",
        "checkpoint_commits": [],
        "attempt": 0,
        "max_attempts": 3,
        "step_baseline_sha": "",
        "attempt_artifacts": [],
        "last_failed_patch_path": "",
        "final_baseline_sha": "",
        "final_attempt": 0,
        "max_final_attempts": 3,
        "last_failed_final_patch_path": "",
        "final_validation_status": "not_started",
        "final_validation_exit_code": 0,
        "final_validation_output": "",
        "final_review_status": "not_started",
        "final_review": {},
        "final_review_error": "",
        "final_commit_sha": None,
        "environment_output": "",
        "environment_ready": False,
        "validation_status": "not_started",
        "validation_exit_code": 0,
        "test_output": "",
        "tests_passed": False,
        "review_status": "not_started",
        "review": {},
        "review_markdown": "",
        "review_published": False,
        "review_error": "",
        "coder_summary": "",
        "coder_error": "",
        "commit_sha": None,
        "pull_request_number": 0,
        "pull_request_url": "",
        "side_effect_intent": {},
        "side_effect_history": [],
        "ci_status": "not_started",
        "ci_run_id": 0,
        "ci_url": "",
        "ci_output": "",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


class GraphEndToEndTests(unittest.TestCase):
    def test_successful_issue_reaches_draft_pull_request(self) -> None:
        fake_client = FakeGitHubClient()
        plan = ImplementationPlan(
            goal="Add a deterministic fixture.",
            summary="Create and validate one fixture.",
            assumptions=[],
            open_questions=[],
            acceptance_criteria=["The fixture is available."],
            steps=[
                PlanStep(
                    id="step-01",
                    title="Add fixture",
                    goal="Create the fixture.",
                    requirements=["Add the fixture."],
                    acceptance_criteria=["The fixture exists."],
                    validation=["Run repository validation."],
                    affected_areas=["fixtures"],
                    out_of_scope=[],
                    depends_on=[],
                )
            ],
        )
        approved_review = ReviewResult(
            status="approved",
            summary="Implementation satisfies the plan.",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            checkpoint_db = root / "checkpoints.db"

            with ExitStack() as stack:
                stack.enter_context(
                    patch.dict(
                        "os.environ",
                        {
                            "CHECKPOINT_DB": str(checkpoint_db),
                            "PUBLISH_PLAN_COMMENT": "true",
                            "PUBLISH_REVIEW_COMMENT": "true",
                        },
                    )
                )
                stack.enter_context(
                    patch("app.graph.GitHubAppClient", return_value=fake_client)
                )
                stack.enter_context(
                    patch(
                        "app.graph.prepare_workspace",
                        return_value=(workspace, "agent/issue-42"),
                    )
                )
                stack.enter_context(
                    patch(
                        "app.graph.collect_repository_context",
                        return_value="Repository fixture context",
                    )
                )
                stack.enter_context(
                    patch("app.graph.create_plan", return_value=plan)
                )
                start_mock = stack.enter_context(
                    patch(
                        "app.graph.start_environment",
                        return_value={
                            "success": True,
                            "exit_code": 0,
                            "output": "environment ready",
                        },
                    )
                )
                stop_mock = stack.enter_context(
                    patch(
                        "app.graph.stop_environment",
                        return_value={
                            "success": True,
                            "exit_code": 0,
                            "output": "environment stopped",
                        },
                    )
                )
                validation_mock = stack.enter_context(
                    patch(
                        "app.graph.run_validation",
                        return_value={
                            "success": True,
                            "exit_code": 0,
                            "output": "validation passed",
                        },
                    )
                )
                coder_mock = stack.enter_context(
                    patch(
                        "app.graph.run_coder",
                        return_value="Implemented the fixture.",
                    )
                )
                review_mock = stack.enter_context(
                    patch(
                        "app.graph.review_implementation",
                        return_value=approved_review,
                    )
                )
                stack.enter_context(
                    patch("app.graph.workspace_has_changes", return_value=False)
                )
                stack.enter_context(
                    patch("app.graph.current_head", return_value="baseline-sha")
                )
                checkpoint_mock = stack.enter_context(
                    patch("app.graph.commit_step", return_value="checkpoint-sha")
                )
                finalization_mock = stack.enter_context(
                    patch(
                        "app.graph.finalize_checkpoint_history",
                        return_value="final-sha",
                    )
                )

                def record_push(*args, **kwargs) -> None:
                    fake_client.branch_heads[
                        "agent/issue-42"
                    ] = "final-sha"

                push_mock = stack.enter_context(
                    patch(
                        "app.graph.push_branch",
                        side_effect=record_push,
                    )
                )

                graph = build_graph()
                try:
                    result = graph.invoke(
                        initial_state(42),
                        config={
                            "configurable": {
                                "thread_id": "test-successful-issue"
                            }
                        },
                    )
                finally:
                    close_graph(graph)

        self.assertEqual(result["workflow_status"], "completed")
        self.assertEqual(result["completed_steps"], ["step-01"])
        self.assertEqual(result["steps"][0]["status"], "completed")
        self.assertEqual(result["final_commit_sha"], "final-sha")
        self.assertEqual(result["pull_request_number"], 7)
        self.assertEqual(
            result["pull_request_url"],
            "https://example.test/pull/7",
        )
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["blocked_stage"], "")

        start_mock.assert_called_once()
        stop_mock.assert_called_once()
        coder_mock.assert_called_once()
        self.assertEqual(validation_mock.call_count, 2)
        self.assertEqual(review_mock.call_count, 2)
        checkpoint_mock.assert_called_once()
        finalization_mock.assert_called_once()
        push_mock.assert_called_once()
        self.assertEqual(len(result["side_effect_history"]), 2)
        self.assertEqual(result["side_effect_intent"], {})
        self.assertEqual(len(fake_client.comments), 2)
        self.assertEqual(len(fake_client.created_pull_requests), 1)
        created_pr = fake_client.created_pull_requests[0]
        self.assertEqual(created_pr["head"], "agent/issue-42")
        self.assertEqual(created_pr["base"], "main")
        self.assertIn("Final commit: `final-sha`", created_pr["body"])


if __name__ == "__main__":
    unittest.main()
