from __future__ import annotations

import unittest

from app.graph import (
    route_after_coder,
    route_after_create_draft_pr,
    route_after_environment,
    route_after_plan_publication,
    route_after_planner,
    route_after_push_branch,
    route_after_review_publication,
    route_after_reviewer,
    route_after_step_completion,
    route_after_validation,
)


class GraphRoutingTests(unittest.TestCase):
    def test_route_after_planner(self) -> None:
        self.assertEqual(
            route_after_planner({"planning_error": "planner crashed"}),
            "planning_failure",
        )
        self.assertEqual(
            route_after_planner({"planning_error": ""}),
            "publish_plan",
        )

    def test_route_after_plan_publication(self) -> None:
        self.assertEqual(
            route_after_plan_publication({"requires_user_input": True}),
            "awaiting_user_input",
        )
        self.assertEqual(
            route_after_plan_publication({"requires_user_input": False}),
            "prepare_workspace",
        )

    def test_route_after_coder(self) -> None:
        self.assertEqual(
            route_after_coder({"coder_error": "edit failed"}),
            "blocked",
        )
        self.assertEqual(
            route_after_coder({"coder_error": ""}),
            "run_validation",
        )

    def test_route_after_environment(self) -> None:
        self.assertEqual(
            route_after_environment({"environment_ready": True}),
            "prepare_current_step",
        )
        self.assertEqual(
            route_after_environment({"environment_ready": False}),
            "environment_failure",
        )

    def test_route_after_validation(self) -> None:
        self.assertEqual(
            route_after_validation(
                {
                    "validation_status": "validation_success",
                    "attempt": 1,
                    "max_attempts": 3,
                }
            ),
            "reviewer",
        )
        self.assertEqual(
            route_after_validation(
                {
                    "validation_status": "project_validation_failure",
                    "attempt": 1,
                    "max_attempts": 3,
                }
            ),
            "coder",
        )
        self.assertEqual(
            route_after_validation(
                {
                    "validation_status": "project_validation_failure",
                    "attempt": 3,
                    "max_attempts": 3,
                }
            ),
            "blocked",
        )

    def test_route_after_reviewer(self) -> None:
        self.assertEqual(
            route_after_reviewer({"review_status": "review_failure"}),
            "blocked",
        )
        self.assertEqual(
            route_after_reviewer({"review_status": "approved"}),
            "publish_review",
        )

    def test_route_after_review_publication(self) -> None:
        self.assertEqual(
            route_after_review_publication(
                {
                    "review_status": "approved",
                    "attempt": 1,
                    "max_attempts": 3,
                }
            ),
            "complete_step",
        )
        self.assertEqual(
            route_after_review_publication(
                {
                    "review_status": "changes_required",
                    "attempt": 1,
                    "max_attempts": 3,
                }
            ),
            "coder",
        )
        self.assertEqual(
            route_after_review_publication(
                {
                    "review_status": "changes_required",
                    "attempt": 3,
                    "max_attempts": 3,
                }
            ),
            "blocked",
        )

    def test_route_after_step_completion(self) -> None:
        self.assertEqual(
            route_after_step_completion(
                {"current_step": 0, "steps": [{"id": "step-1"}]}
            ),
            "prepare_current_step",
        )
        self.assertEqual(
            route_after_step_completion(
                {"current_step": 1, "steps": [{"id": "step-1"}]}
            ),
            "workflow_complete",
        )

    def test_route_after_push_branch(self) -> None:
        self.assertEqual(
            route_after_push_branch({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_push_branch({"workflow_status": "implementing"}),
            "create_draft_pr",
        )

    def test_route_after_create_draft_pr(self) -> None:
        self.assertEqual(
            route_after_create_draft_pr({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_create_draft_pr({"workflow_status": "completed"}),
            "cleanup",
        )


if __name__ == "__main__":
    unittest.main()
