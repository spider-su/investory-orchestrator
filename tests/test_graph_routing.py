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
    route_after_workflow_complete,
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
                {"validation_status": "validation_success", "workflow_mode": "legacy"}
            ),
            "reviewer",
        )
        self.assertEqual(
            route_after_validation(
                {"validation_status": "validation_success", "workflow_mode": "simplified"}
            ),
            "prepare_checkpoint",
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
                {
                    "workflow_status": "blocked",
                    "current_step": 0,
                    "steps": [{"id": "step-1"}],
                }
            ),
            "blocked",
        )
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
        self.assertEqual(
            route_after_final_validation(
                {
                    "workflow_mode": "simplified",
                    "attempt": 2,
                    "final_attempt": 0,
                    "final_validation_status": "project_validation_failure",
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
        self.assertEqual(
            route_after_final_reviewer(
                {
                    "workflow_mode": "simplified",
                    "attempt": 2,
                    "final_attempt": 0,
                    "final_review_status": "changes_required",
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

    def test_workflow_complete_skips_pr_for_approved_no_change_outcome(self) -> None:
        self.assertEqual(
            route_after_workflow_complete({"no_change_outcome": True}),
            "cleanup",
        )
        self.assertEqual(
            route_after_workflow_complete({"no_change_outcome": False}),
            "prepare_push_branch",
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

    def test_blocked_push_resume_retries_push_after_preparation(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "push_branch",
                }
            ),
            "prepare_push_branch",
        )

    def test_cleanup_resume_retries_cleanup_after_blocked_node(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "cleanup",
                }
            ),
            "blocked",
        )

    def test_environment_resume_restarts_workspace_preparation(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "environment",
                }
            ),
            "load_issue",
        )

    def test_coder_resume_restarts_environment_before_coder(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "coder",
                }
            ),
            "resume_environment",
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
            "prepare_push_branch",
        )

    def test_checkpoint_commit_resume_retries_the_commit_node(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "complete_step",
                }
            ),
            "prepare_checkpoint",
        )

    def test_finalization_resume_retries_finalizer_after_intent_preparation(self) -> None:
        self.assertEqual(
            resolve_resume_from(
                {"workflow_status": "blocked", "blocked_stage": "finalize_history"}
            ),
            "prepare_finalize_history",
        )
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "publishing",
                    "side_effect_intent": {
                        "kind": "finalization",
                        "status": "prepared",
                        "operation_id": "issue-42:finalize",
                    },
                }
            ),
            "prepare_finalize_history",
        )


if __name__ == "__main__":
    unittest.main()
