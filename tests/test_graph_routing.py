from __future__ import annotations

import unittest

from app.graph import (
    route_after_coder,
    route_after_create_draft_pr,
    route_after_environment,
    route_after_failed_attempt,
    route_after_final_failed_attempt,
    route_after_final_integration_coder,
    route_after_final_reviewer,
    route_after_final_validation,
    route_after_finalize_history,
    route_after_plan_publication,
    route_after_planner,
    route_after_prepare_draft_pr,
    route_after_prepare_final_review,
    route_after_prepare_push_branch,
    route_after_push_branch,
    route_after_review_publication,
    route_after_reviewer,
    route_after_step_completion,
    route_after_validation,
    resolve_resume_from,
)


class GraphRoutingTests(unittest.TestCase):
    def test_route_after_planner(self) -> None:
        self.assertEqual(
            route_after_planner({"planning_error": "planner crashed"}),
            "planning_failure",
        )
        self.assertEqual(
            route_after_planner({"planning_error": ""}),
        "prepare_plan_comment",
        )

    def test_route_after_plan_publication(self) -> None:
        self.assertEqual(
            route_after_plan_publication({"requires_user_input": True}),
            "awaiting_user_input",
        )
        self.assertEqual(
            route_after_plan_publication({"requires_user_input": False}),
            "start_environment",
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
                {"validation_status": "validation_success"}
            ),
            "reviewer",
        )
        self.assertEqual(
            route_after_validation(
                {"validation_status": "project_validation_failure"}
            ),
            "isolate_validation_failure",
        )
        self.assertEqual(
            route_after_validation(
                {"validation_status": "environment_failure"}
            ),
            "environment_failure",
        )

    def test_route_after_failed_attempt(self) -> None:
        self.assertEqual(
            route_after_failed_attempt(
                {"error": "isolation failed", "attempt": 1, "max_attempts": 3}
            ),
            "blocked",
        )
        self.assertEqual(
            route_after_failed_attempt(
                {"error": "", "attempt": 1, "max_attempts": 3}
            ),
            "coder",
        )
        self.assertEqual(
            route_after_failed_attempt(
                {"error": "", "attempt": 3, "max_attempts": 3}
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
        "prepare_review_comment",
        )

    def test_route_after_review_publication(self) -> None:
        self.assertEqual(
            route_after_review_publication({"review_status": "approved"}),
        "prepare_checkpoint",
        )
        self.assertEqual(
            route_after_review_publication(
                {"review_status": "changes_required"}
            ),
            "isolate_review_failure",
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
            "prepare_final_review",
        )

    def test_route_after_prepare_final_review(self) -> None:
        self.assertEqual(
            route_after_prepare_final_review(
                {"workflow_status": "blocked"}
            ),
            "blocked",
        )
        self.assertEqual(
            route_after_prepare_final_review(
                {"workflow_status": "validating"}
            ),
            "final_validation",
        )

    def test_route_after_final_validation(self) -> None:
        self.assertEqual(
            route_after_final_validation(
                {
                    "final_validation_status": "environment_failure",
                    "final_attempt": 0,
                }
            ),
            "environment_failure",
        )
        self.assertEqual(
            route_after_final_validation(
                {
                    "final_validation_status": "validation_success",
                    "final_attempt": 0,
                }
            ),
            "final_reviewer",
        )
        self.assertEqual(
            route_after_final_validation(
                {
                    "final_validation_status": "project_validation_failure",
                    "final_attempt": 0,
                }
            ),
            "final_integration_coder",
        )
        self.assertEqual(
            route_after_final_validation(
                {
                    "final_validation_status": "project_validation_failure",
                    "final_attempt": 1,
                }
            ),
            "isolate_final_validation_failure",
        )

    def test_route_after_final_integration_coder(self) -> None:
        self.assertEqual(
            route_after_final_integration_coder(
                {"coder_error": "repair failed"}
            ),
            "blocked",
        )
        self.assertEqual(
            route_after_final_integration_coder({"coder_error": ""}),
            "final_validation",
        )

    def test_route_after_final_reviewer(self) -> None:
        self.assertEqual(
            route_after_final_reviewer(
                {"final_review_status": "review_failure", "final_attempt": 0}
            ),
            "blocked",
        )
        self.assertEqual(
            route_after_final_reviewer(
                {"final_review_status": "approved", "final_attempt": 0}
            ),
        "prepare_finalize_history",
        )
        self.assertEqual(
            route_after_final_reviewer(
                {
                    "final_review_status": "changes_required",
                    "final_attempt": 0,
                }
            ),
            "final_integration_coder",
        )
        self.assertEqual(
            route_after_final_reviewer(
                {
                    "final_review_status": "changes_required",
                    "final_attempt": 1,
                }
            ),
            "isolate_final_review_failure",
        )

    def test_route_after_final_failed_attempt(self) -> None:
        self.assertEqual(
            route_after_final_failed_attempt(
                {
                    "error": "isolation failed",
                    "final_attempt": 1,
                    "max_final_attempts": 3,
                }
            ),
            "blocked",
        )
        self.assertEqual(
            route_after_final_failed_attempt(
                {"error": "", "final_attempt": 1, "max_final_attempts": 3}
            ),
            "final_integration_coder",
        )
        self.assertEqual(
            route_after_final_failed_attempt(
                {"error": "", "final_attempt": 3, "max_final_attempts": 3}
            ),
            "blocked",
        )

    def test_route_after_finalize_history(self) -> None:
        self.assertEqual(
            route_after_finalize_history({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_finalize_history({"workflow_status": "reviewing"}),
            "workflow_complete",
        )

    def test_route_after_prepare_push_branch(self) -> None:
        self.assertEqual(
            route_after_prepare_push_branch({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_prepare_push_branch(
                {"workflow_status": "publishing"}
            ),
            "push_branch",
        )

    def test_route_after_push_branch(self) -> None:
        self.assertEqual(
            route_after_push_branch({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_push_branch({"workflow_status": "implementing"}),
            "prepare_draft_pr",
        )

    def test_route_after_prepare_draft_pr(self) -> None:
        self.assertEqual(
            route_after_prepare_draft_pr({"workflow_status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            route_after_prepare_draft_pr(
                {"workflow_status": "publishing"}
            ),
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

    def test_push_resume_uses_saved_intent_without_repreparing(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "push_branch",
                }
            ),
            "push_branch",
        )

    def test_coder_resume_preserves_attempt_state(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "coder",
                }
            ),
            "prepare_current_step",
        )
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "publishing",
                    "side_effect_intent": {
                        "status": "prepared",
                        "kind": "push_branch",
                        "operation_id": "operation",
                    },
                }
            ),
            "push_branch",
        )


if __name__ == "__main__":
    unittest.main()
