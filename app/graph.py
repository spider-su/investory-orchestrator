from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agents.coder import CoderError, run_coder
from app.agents.planner import (
    PlannerError,
    create_plan,
    plan_to_markdown,
)
from app.agents.reviewer import (
    ReviewerError,
    review_implementation,
    review_to_markdown,
)
from app.github_client import GitHubAppClient
from app.repository_context import collect_repository_context
from app.retry_isolation import (
    RetryIsolationError,
    archive_and_reset_failed_attempt,
    current_head,
    workspace_has_changes,
)
from app.side_effects import (
    complete_intent,
    has_prepared_intent,
    prepare_draft_pr_intent,
    prepare_push_intent,
)
from app.state import WorkflowState
from app.test_runner import (
    run_validation,
    start_environment,
    stop_environment,
)
from app.workspace import (
    commit_step,
    finalize_checkpoint_history,
    prepare_workspace,
    push_branch,
)


def load_issue(state: WorkflowState) -> dict:
    client = GitHubAppClient()
    issue = client.get_issue(state["issue_number"])

    print(f"Loaded issue #{issue.number}: {issue.title}")

    return {
        "issue_title": issue.title,
        "issue_body": issue.body or "",
        "error": "",
    }


def planner_node(state: WorkflowState) -> dict:
    print("Creating implementation plan")

    try:
        plan = create_plan(
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            repository_context=state["repository_context"],
        )
    except PlannerError as error:
        message = str(error)
        print("Planning failed")
        print(message)

        return {
            "plan": {},
            "plan_markdown": "",
            "plan_published": False,
            "planning_error": message,
            "requires_user_input": False,
            "error": message,
        }

    markdown = plan_to_markdown(plan)
    requires_user_input = bool(plan.open_questions)

    print(
        f"Plan created with {len(plan.steps)} step(s)"
    )

    if requires_user_input:
        print(
            f"Plan contains "
            f"{len(plan.open_questions)} open question(s)"
        )

    plan_dict = plan.model_dump(mode="json")
    steps = [
        {
            **step,
            "status": "pending",
            "attempts": 0,
            "commit_sha": None,
        }
        for step in plan_dict["steps"]
    ]

    return {
        "workflow_status": "planning",
        "plan": plan_dict,
        "steps": steps,
        "plan_markdown": markdown,
        "plan_published": False,
        "planning_error": "",
        "requires_user_input": requires_user_input,
        "current_step": 0,
        "completed_steps": [],
        "error": "",
    }


def publish_plan_node(state: WorkflowState) -> dict:
    publish_enabled = (
        os.getenv("PUBLISH_PLAN_COMMENT", "true").lower()
        in {"1", "true", "yes"}
    )

    if not publish_enabled:
        print("Plan publication disabled")
        return {"plan_published": False}

    client = GitHubAppClient()

    client.add_issue_comment(
        state["issue_number"],
        state["plan_markdown"],
    )

    print("Plan published to GitHub issue")

    return {"plan_published": True}


def route_after_planner(state: WorkflowState) -> str:
    if state["planning_error"]:
        return "planning_failure"

    return "publish_plan"


def route_after_plan_publication(
    state: WorkflowState,
) -> str:
    if state["requires_user_input"]:
        return "awaiting_user_input"

    return "start_environment"


def planning_failure_node(state: WorkflowState) -> dict:
    print("RESULT: PLANNING FAILURE")
    print(state["planning_error"])
    return {}


def awaiting_user_input_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: USER INPUT REQUIRED")

    questions = state["plan"].get(
        "open_questions",
        [],
    )

    for question in questions:
        print(f"- {question}")

    blocked_reason = (
        "Planner requires user input before implementation can continue."
    )

    if questions:
        blocked_reason = (
            f"{blocked_reason}\n"
            + "\n".join(f"- {question}" for question in questions)
        )

    return {
        "workflow_status": "blocked",
        "blocked_reason": blocked_reason,
        "blocked_stage": "awaiting_user_input",
        "error": "",
    }


def resume_from_for_stage(blocked_stage: str) -> str | None:
    return {
        "awaiting_user_input": "prepare_workspace",
        "environment": "prepare_workspace",
        "coder": "prepare_current_step",
        "reviewer": "run_validation",
        "prepare_final_review": "complete_step",
        "final_integration_coder": "prepare_final_review",
        "final_reviewer": "final_validation",
        "finalize_history": "final_reviewer",
        "prepare_push_branch": "workflow_complete",
        "push_branch": "prepare_push_branch",
        "prepare_draft_pr": "push_branch",
        "create_draft_pr": "prepare_draft_pr",
    }.get(blocked_stage)


def resolve_resume_from(state: dict) -> str:
    workflow_status = state.get("workflow_status")

    if workflow_status == "blocked":
        blocked_stage = state.get("blocked_stage", "")
        resume_from = resume_from_for_stage(blocked_stage)

        if resume_from is None:
            raise RuntimeError(
                "Blocked workflow does not contain a resumable stage. "
                f"Blocked stage: {blocked_stage or 'missing'}"
            )

        return resume_from

    if has_prepared_intent(state):
        intent = state["side_effect_intent"]
        resume_from = {
            "push_branch": "prepare_push_branch",
            "draft_pr_upsert": "prepare_draft_pr",
        }.get(intent.get("kind"))

        if resume_from is not None:
            return resume_from

    raise RuntimeError(
        "Only blocked workflows or workflows with a prepared remote "
        f"side effect can be resumed. Current status: {workflow_status}"
    )


def reload_issue_for_planning(issue_number: int) -> dict:
    client = GitHubAppClient()
    issue = client.get_issue(issue_number)

    print(f"Reloaded issue #{issue.number}: {issue.title}")

    return {
        "issue_title": issue.title,
        "issue_body": issue.body or "",
        "workflow_status": "planning",
        "plan": {},
        "plan_markdown": "",
        "plan_published": False,
        "planning_error": "",
        "requires_user_input": False,
        "steps": [],
        "current_step": 0,
        "completed_steps": [],
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def prepare_workspace_node(state: WorkflowState) -> dict:
    client = GitHubAppClient()

    workspace, branch = prepare_workspace(
        client,
        state["issue_number"],
    )

    print(f"Workspace prepared: {workspace}")
    print(f"Branch prepared: {branch}")

    issue_baseline_sha = (
        state.get("issue_baseline_sha")
        or current_head(workspace)
    )

    return {
        "workspace": str(workspace),
        "branch": branch,
        "issue_baseline_sha": issue_baseline_sha,
    }


def collect_repository_context_node(
    state: WorkflowState,
) -> dict:
    workspace = Path(state["workspace"])
    context = collect_repository_context(workspace)

    print(
        f"Repository context collected: {len(context)} characters"
    )

    return {"repository_context": context}


def start_environment_node(state: WorkflowState) -> dict:
    print("Starting Dev Container environment")

    result = start_environment(
        Path(state["workspace"]),
        state["issue_number"],
    )

    if result["success"]:
        print("Environment ready")

        return {
            "environment_ready": True,
            "environment_output": result["output"],
            "validation_status": "not_started",
            "validation_exit_code": 0,
            "error": "",
        }

    print("Environment setup failed")

    return {
        "environment_ready": False,
        "environment_output": result["output"],
        "validation_status": "environment_failure",
        "validation_exit_code": result["exit_code"],
        "error": result["output"],
    }


def prepare_current_step_node(state: WorkflowState) -> dict:
    index = state["current_step"]
    steps = [dict(step) for step in state["steps"]]

    if index >= len(steps):
        raise RuntimeError("Current step is outside the implementation plan")

    steps[index]["status"] = "in_progress"
    step = steps[index]

    workspace = Path(state["workspace"])
    baseline_sha = current_head(workspace)
    attempt_artifacts = list(state.get("attempt_artifacts", []))

    if workspace_has_changes(workspace):
        artifact = archive_and_reset_failed_attempt(
            workspace=workspace,
            issue_number=state["issue_number"],
            step_id=step["id"],
            attempt=0,
            failure_stage="pre-step-workspace",
            baseline_sha=baseline_sha,
            coder_summary="Uncommitted changes existed before the step started.",
            validation_output="",
            validation_exit_code=0,
            review={},
        )
        attempt_artifacts.append(artifact)
        print(
            "Archived and removed pre-existing workspace changes: "
            f"{artifact['patch_path']}"
        )

    print(
        f"Starting step {index + 1}/{len(steps)}: "
        f"{step['id']} — {step['title']}"
    )

    return {
        "workflow_status": "implementing",
        "steps": steps,
        "attempt": 0,
        "step_baseline_sha": baseline_sha,
        "attempt_artifacts": attempt_artifacts,
        "last_failed_patch_path": "",
        "coder_summary": "",
        "coder_error": "",
        "validation_status": "not_started",
        "validation_exit_code": 0,
        "test_output": "",
        "tests_passed": False,
        "review_status": "not_started",
        "review": {},
        "review_markdown": "",
        "review_error": "",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def coder_node(state: WorkflowState) -> dict:
    next_attempt = state["attempt"] + 1
    step = state["steps"][state["current_step"]]

    print(
        f"Running coder attempt "
        f"{next_attempt}/{state['max_attempts']}"
    )

    try:
        summary = run_coder(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            step=step,
            validation_output=state["test_output"],
            review_feedback=state["review"],
            attempt=next_attempt,
            max_attempts=state["max_attempts"],
            failed_patch_path=state.get("last_failed_patch_path", ""),
        )
    except CoderError as error:
        message = str(error)
        print("Coder failed")
        print(message[-4000:])

        artifacts = list(state.get("attempt_artifacts", []))
        last_failed_patch_path = state.get(
            "last_failed_patch_path",
            "",
        )
        workspace = Path(state["workspace"])

        if workspace_has_changes(workspace):
            try:
                artifact = archive_and_reset_failed_attempt(
                    workspace=workspace,
                    issue_number=state["issue_number"],
                    step_id=step["id"],
                    attempt=next_attempt,
                    failure_stage="coder",
                    baseline_sha=(
                        state.get("step_baseline_sha")
                        or current_head(workspace)
                    ),
                    coder_summary="",
                    validation_output="",
                    validation_exit_code=0,
                    review={},
                )
                artifacts.append(artifact)
                last_failed_patch_path = artifact["patch_path"]
            except RetryIsolationError as isolation_error:
                message = (
                    f"{message}\n\n"
                    "Failed to isolate partial coder changes:\n"
                    f"{isolation_error}"
                )

        return {
            "coder_summary": "",
            "coder_error": message,
            "attempt_artifacts": artifacts,
            "last_failed_patch_path": last_failed_patch_path,
            "blocked_reason": message,
            "blocked_stage": "coder",
            "error": message,
        }

    print("Coder completed")

    return {
        "workflow_status": "implementing",
        "attempt": next_attempt,
        "review": {},
        "review_status": "not_started",
        "coder_summary": summary,
        "coder_error": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_coder(state: WorkflowState) -> str:
    if state["coder_error"]:
        return "blocked"

    return "run_validation"


def route_after_environment(state: WorkflowState) -> str:
    if state["environment_ready"]:
        return "prepare_current_step"

    return "environment_failure"


def run_validation_node(state: WorkflowState) -> dict:
    print("Running project validation")

    result = run_validation(
        Path(state["workspace"]),
        state["issue_number"],
    )

    if result["success"]:
        print("Validation succeeded")

        return {
            "workflow_status": "validating",
            "validation_status": "validation_success",
            "validation_exit_code": 0,
            "test_output": result["output"],
            "tests_passed": True,
            "error": "",
        }

    print("Project validation failed")

    return {
        "workflow_status": "validating",
        "validation_status": "project_validation_failure",
        "validation_exit_code": result["exit_code"],
        "test_output": result["output"],
        "tests_passed": False,
        "blocked_reason": (
            f"Validation failed after {state['attempt']} attempts."
            if state["attempt"] >= state["max_attempts"]
            else ""
        ),
        "blocked_stage": "coder",
        "error": "",
    }


def route_after_validation(state: WorkflowState) -> str:
    if state["validation_status"] == "validation_success":
        return "reviewer"

    return "isolate_validation_failure"


def _isolate_failed_attempt(
    state: WorkflowState,
    *,
    failure_stage: str,
) -> dict:
    workspace = Path(state["workspace"])
    step = state["steps"][state["current_step"]]
    baseline_sha = (
        state.get("step_baseline_sha")
        or current_head(workspace)
    )

    try:
        artifact = archive_and_reset_failed_attempt(
            workspace=workspace,
            issue_number=state["issue_number"],
            step_id=step["id"],
            attempt=state["attempt"],
            failure_stage=failure_stage,
            baseline_sha=baseline_sha,
            coder_summary=state["coder_summary"],
            validation_output=state["test_output"],
            validation_exit_code=state["validation_exit_code"],
            review=state["review"],
        )
    except RetryIsolationError as error:
        message = (
            "Failed to preserve and reset the failed attempt. "
            "The workspace was not intentionally discarded.\n"
            f"{error}"
        )
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "coder",
            "error": message,
        }

    print(
        f"Archived failed {failure_stage} attempt: "
        f"{artifact['patch_path']}"
    )
    print(f"Workspace reset to step baseline {baseline_sha}")

    exhausted = state["attempt"] >= state["max_attempts"]
    reason = ""

    if exhausted:
        reason = (
            f"{failure_stage.capitalize()} failed after "
            f"{state['attempt']} implementation attempts. "
            f"Last failed patch: {artifact['patch_path']}"
        )

    return {
        "attempt_artifacts": [
            *state.get("attempt_artifacts", []),
            artifact,
        ],
        "last_failed_patch_path": artifact["patch_path"],
        "blocked_reason": reason,
        "blocked_stage": "coder" if exhausted else "",
        "error": "",
    }


def isolate_validation_failure_node(
    state: WorkflowState,
) -> dict:
    return _isolate_failed_attempt(
        state,
        failure_stage="validation",
    )


def isolate_review_failure_node(
    state: WorkflowState,
) -> dict:
    return _isolate_failed_attempt(
        state,
        failure_stage="review",
    )


def route_after_failed_attempt(state: WorkflowState) -> str:
    if state["error"]:
        return "blocked"

    if state["attempt"] < state["max_attempts"]:
        return "coder"

    return "blocked"


def cleanup_node(state: WorkflowState) -> dict:
    print("Stopping Dev Container environment")

    result = stop_environment(
        Path(state["workspace"]),
        state["issue_number"],
    )

    if not result["success"]:
        print("Cleanup warning:")
        print(result["output"][-2000:])

    return {}


def environment_failure_node(state: WorkflowState) -> dict:
    print("RESULT: ENVIRONMENT FAILURE")
    print(state["environment_output"][-4000:])
    return {
        "blocked_reason": state["environment_output"],
        "blocked_stage": "environment",
    }


def project_validation_failure_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: PROJECT VALIDATION FAILURE")
    print(state["test_output"][-4000:])
    return {}


def validation_success_node(state: WorkflowState) -> dict:
    print("RESULT: VALIDATION SUCCESS")
    return {}


def reviewer_node(state: WorkflowState) -> dict:
    print("Reviewing implementation")
    step = state["steps"][state["current_step"]]

    try:
        review = review_implementation(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            plan={
                "overall_acceptance_criteria": state["plan"].get(
                    "acceptance_criteria",
                    [],
                ),
                "current_step": step,
            },
            validation_output=state["test_output"],
        )
    except ReviewerError as error:
        message = str(error)

        print("Review failed")
        print(message[-4000:])

        return {
            "review_status": "review_failure",
            "review": {},
            "review_markdown": "",
            "review_published": False,
            "review_error": message,
            "blocked_reason": message,
            "blocked_stage": "reviewer",
            "error": message,
        }

    markdown = review_to_markdown(review)

    print(f"Review result: {review.status}")

    return {
        "workflow_status": "reviewing",
        "review_status": review.status,
        "review": review.model_dump(mode="json"),
        "review_markdown": markdown,
        "review_published": False,
        "review_error": "",
        "error": "",
    }


def publish_review_node(state: WorkflowState) -> dict:
    publish_enabled = (
        os.getenv(
            "PUBLISH_REVIEW_COMMENT",
            "true",
        ).lower()
        in {"1", "true", "yes"}
    )

    if not publish_enabled:
        print("Review publication disabled")
        return {"review_published": False}

    client = GitHubAppClient()

    client.add_issue_comment(
        state["issue_number"],
        state["review_markdown"],
    )

    print("Review published to GitHub issue")

    return {
        "review_published": True,
        "blocked_stage": (
            "coder" if state["review_status"] == "changes_required" else ""
        ),
    }


def route_after_reviewer(
    state: WorkflowState,
) -> str:
    if state["review_status"] == "review_failure":
        return "blocked"

    return "publish_review"


def route_after_review_publication(
    state: WorkflowState,
) -> str:
    if state["review_status"] == "approved":
        return "complete_step"

    return "isolate_review_failure"


def complete_step_node(state: WorkflowState) -> dict:
    index = state["current_step"]
    steps = [dict(step) for step in state["steps"]]
    step = steps[index]

    commit_sha = commit_step(
        Path(state["workspace"]),
        step["id"],
        step["title"],
    )

    step["status"] = "completed"
    step["attempts"] = state["attempt"]
    step["commit_sha"] = commit_sha

    if commit_sha is None:
        print(
            f"Completed {step['id']} without creating a new commit"
        )
    else:
        print(f"Completed {step['id']} at {commit_sha}")

    return {
        "steps": steps,
        "completed_steps": [
            *state["completed_steps"],
            step["id"],
        ],
        "current_step": index + 1,
        "attempt": 0,
        "step_baseline_sha": "",
        "last_failed_patch_path": "",
        "commit_sha": commit_sha,
        "checkpoint_commits": (
            [*state.get("checkpoint_commits", []), commit_sha]
            if commit_sha
            else list(state.get("checkpoint_commits", []))
        ),
    }


def route_after_step_completion(state: WorkflowState) -> str:
    if state["current_step"] >= len(state["steps"]):
        return "prepare_final_review"

    return "prepare_current_step"


def prepare_final_review_node(state: WorkflowState) -> dict:
    workspace = Path(state["workspace"])

    if workspace_has_changes(workspace):
        message = (
            "The workspace contains uncommitted changes after the final "
            "checkpoint step. Refusing to start whole-plan review."
        )
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "prepare_final_review",
            "error": message,
        }

    baseline_sha = current_head(workspace)
    print(f"Whole-plan checkpoint baseline: {baseline_sha}")

    return {
        "workflow_status": "validating",
        "final_baseline_sha": baseline_sha,
        "final_attempt": 0,
        "last_failed_final_patch_path": "",
        "final_validation_status": "not_started",
        "final_validation_exit_code": 0,
        "final_validation_output": "",
        "final_review_status": "not_started",
        "final_review": {},
        "final_review_error": "",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_prepare_final_review(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "final_validation"


def final_validation_node(state: WorkflowState) -> dict:
    print("Running final whole-plan validation")

    result = run_validation(
        Path(state["workspace"]),
        state["issue_number"],
    )

    if result["success"]:
        print("Final validation succeeded")
        return {
            "workflow_status": "validating",
            "final_validation_status": "validation_success",
            "final_validation_exit_code": 0,
            "final_validation_output": result["output"],
            "blocked_reason": "",
            "blocked_stage": "",
            "error": "",
        }

    print("Final validation failed")
    return {
        "workflow_status": "validating",
        "final_validation_status": "project_validation_failure",
        "final_validation_exit_code": result["exit_code"],
        "final_validation_output": result["output"],
        "error": "",
    }


def route_after_final_validation(state: WorkflowState) -> str:
    if (
        state["final_validation_status"]
        == "validation_success"
    ):
        return "final_reviewer"

    if state["final_attempt"] == 0:
        return "final_integration_coder"

    return "isolate_final_validation_failure"


def final_integration_coder_node(state: WorkflowState) -> dict:
    next_attempt = state["final_attempt"] + 1
    max_attempts = state["max_final_attempts"]

    print(
        "Running whole-plan integration repair "
        f"{next_attempt}/{max_attempts}"
    )

    integration_step = {
        "id": "whole-plan-integration",
        "title": "Resolve whole-plan integration findings",
        "goal": (
            "Produce one coherent implementation of the complete issue. "
            "Checkpointed step decisions are provisional and may be revised."
        ),
        "requirements": [
            "Address final validation failures and whole-plan review findings.",
            "Reconsider earlier abstractions when later steps exposed a poor design.",
            "Keep the complete change within the original issue scope.",
            "Preserve all issue-level acceptance criteria.",
        ],
        "acceptance_criteria": state["plan"].get(
            "acceptance_criteria",
            [],
        ),
        "affected_areas": [
            area
            for step in state["steps"]
            for area in step.get("affected_areas", [])
        ],
        "status": "in_progress",
    }

    try:
        summary = run_coder(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            step=integration_step,
            validation_output=state["final_validation_output"],
            review_feedback=state["final_review"],
            attempt=next_attempt,
            max_attempts=max_attempts,
            failed_patch_path=state.get(
                "last_failed_final_patch_path",
                "",
            ),
        )
    except CoderError as error:
        message = str(error)
        workspace = Path(state["workspace"])
        artifacts = list(state.get("attempt_artifacts", []))
        last_patch = state.get(
            "last_failed_final_patch_path",
            "",
        )

        if workspace_has_changes(workspace):
            try:
                artifact = archive_and_reset_failed_attempt(
                    workspace=workspace,
                    issue_number=state["issue_number"],
                    step_id="whole-plan",
                    attempt=next_attempt,
                    failure_stage="final-coder",
                    baseline_sha=state["final_baseline_sha"],
                    coder_summary="",
                    validation_output="",
                    validation_exit_code=0,
                    review={},
                )
                artifacts.append(artifact)
                last_patch = artifact["patch_path"]
            except RetryIsolationError as isolation_error:
                message = (
                    f"{message}\n\n"
                    "Failed to isolate partial whole-plan changes:\n"
                    f"{isolation_error}"
                )

        return {
            "coder_error": message,
            "attempt_artifacts": artifacts,
            "last_failed_final_patch_path": last_patch,
            "blocked_reason": message,
            "blocked_stage": "final_integration_coder",
            "error": message,
        }

    print("Whole-plan integration repair completed")
    return {
        "workflow_status": "implementing",
        "final_attempt": next_attempt,
        "coder_summary": summary,
        "coder_error": "",
        "final_review_status": "not_started",
        "final_review": {},
        "final_review_error": "",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_final_integration_coder(
    state: WorkflowState,
) -> str:
    if state["coder_error"]:
        return "blocked"

    return "final_validation"


def final_reviewer_node(state: WorkflowState) -> dict:
    print("Running final whole-plan review")

    try:
        review = review_implementation(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            plan=state["plan"],
            validation_output=state["final_validation_output"],
            review_scope="whole_plan",
            baseline_sha=state["issue_baseline_sha"],
        )
    except ReviewerError as error:
        message = str(error)
        return {
            "final_review_status": "review_failure",
            "final_review": {},
            "final_review_error": message,
            "blocked_reason": message,
            "blocked_stage": "final_reviewer",
            "error": message,
        }

    print(f"Final review result: {review.status}")
    return {
        "workflow_status": "reviewing",
        "final_review_status": review.status,
        "final_review": review.model_dump(mode="json"),
        "final_review_error": "",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_final_reviewer(state: WorkflowState) -> str:
    if state["final_review_status"] == "review_failure":
        return "blocked"

    if state["final_review_status"] == "approved":
        return "finalize_history"

    if state["final_attempt"] == 0:
        return "final_integration_coder"

    return "isolate_final_review_failure"


def _isolate_final_attempt(
    state: WorkflowState,
    *,
    failure_stage: str,
) -> dict:
    try:
        artifact = archive_and_reset_failed_attempt(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            step_id="whole-plan",
            attempt=state["final_attempt"],
            failure_stage=failure_stage,
            baseline_sha=state["final_baseline_sha"],
            coder_summary=state["coder_summary"],
            validation_output=state["final_validation_output"],
            validation_exit_code=state[
                "final_validation_exit_code"
            ],
            review=state["final_review"],
        )
    except RetryIsolationError as error:
        message = (
            "Failed to preserve and reset the whole-plan repair.\n"
            f"{error}"
        )
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "final_integration_coder",
            "error": message,
        }

    exhausted = (
        state["final_attempt"] >= state["max_final_attempts"]
    )
    reason = ""

    if exhausted:
        reason = (
            f"{failure_stage} failed after "
            f"{state['final_attempt']} whole-plan repair attempts. "
            f"Last failed patch: {artifact['patch_path']}"
        )

    print(
        f"Archived failed {failure_stage} repair: "
        f"{artifact['patch_path']}"
    )

    return {
        "attempt_artifacts": [
            *state.get("attempt_artifacts", []),
            artifact,
        ],
        "last_failed_final_patch_path": artifact["patch_path"],
        "blocked_reason": reason,
        "blocked_stage": (
            "final_integration_coder" if exhausted else ""
        ),
        "error": "",
    }


def isolate_final_validation_failure_node(
    state: WorkflowState,
) -> dict:
    return _isolate_final_attempt(
        state,
        failure_stage="final-validation",
    )


def isolate_final_review_failure_node(
    state: WorkflowState,
) -> dict:
    return _isolate_final_attempt(
        state,
        failure_stage="final-review",
    )


def route_after_final_failed_attempt(
    state: WorkflowState,
) -> str:
    if state["error"]:
        return "blocked"

    if state["final_attempt"] < state["max_final_attempts"]:
        return "final_integration_coder"

    return "blocked"


def finalize_history_node(state: WorkflowState) -> dict:
    print("Replacing checkpoint commits with final logical commit")

    try:
        commit_sha = finalize_checkpoint_history(
            Path(state["workspace"]),
            baseline_sha=state["issue_baseline_sha"],
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
        )
    except RuntimeError as error:
        message = str(error)
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "finalize_history",
            "error": message,
        }

    print(f"Final logical commit: {commit_sha}")
    return {
        "final_commit_sha": commit_sha,
        "commit_sha": commit_sha,
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_finalize_history(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "workflow_complete"


def workflow_complete_node(state: WorkflowState) -> dict:
    print("RESULT: WHOLE-PLAN IMPLEMENTATION APPROVED")
    return {}


def prepare_push_branch_node(state: WorkflowState) -> dict:
    target_sha = state.get("final_commit_sha")

    if not target_sha:
        message = "Cannot prepare branch push without a final commit SHA."
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "prepare_push_branch",
            "error": message,
        }

    try:
        client = GitHubAppClient()
        expected_remote_sha = client.get_branch_head_sha(state["branch"])
    except RuntimeError as error:
        message = str(error)
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "prepare_push_branch",
            "error": message,
        }

    intent = prepare_push_intent(
        issue_number=state["issue_number"],
        branch=state["branch"],
        target_sha=target_sha,
        expected_remote_sha=expected_remote_sha,
    )

    print(f"Prepared remote operation: {intent['operation_id']}")
    return {
        "workflow_status": "publishing",
        "side_effect_intent": intent,
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_prepare_push_branch(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "push_branch"


def push_branch_node(state: WorkflowState) -> dict:
    intent = state.get("side_effect_intent", {})

    if (
        intent.get("status") != "prepared"
        or intent.get("kind") != "push_branch"
    ):
        message = "Branch push is missing a prepared side-effect intent."
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "push_branch",
            "error": message,
        }

    try:
        client = GitHubAppClient()
        branch = state["branch"]
        target_sha = intent["target_sha"]
        expected_remote_sha = (
            intent.get("expected_remote_sha") or None
        )
        remote_sha = client.get_branch_head_sha(branch)

        if remote_sha == target_sha:
            history = complete_intent(
                intent,
                list(state.get("side_effect_history", [])),
                remote_sha=remote_sha,
                reconciled=True,
            )
            print(f"Reconciled already-pushed branch: {branch}")
            return {
                "workflow_status": "publishing",
                "side_effect_intent": {},
                "side_effect_history": history,
                "blocked_reason": "",
                "blocked_stage": "",
                "error": "",
            }

        if remote_sha != expected_remote_sha:
            expected_display = expected_remote_sha or "<absent>"
            actual_display = remote_sha or "<absent>"
            message = (
                f"Remote branch '{branch}' moved from expected "
                f"{expected_display} to {actual_display}; "
                "refusing to overwrite it."
            )
            return {
                "workflow_status": "blocked",
                "blocked_reason": message,
                "blocked_stage": "push_branch",
                "error": message,
            }

        push_branch(
            client,
            Path(state["workspace"]),
            branch,
            expected_remote_sha=expected_remote_sha,
        )

        remote_sha = client.get_branch_head_sha(branch)
        if remote_sha != target_sha:
            raise RuntimeError(
                "Branch push returned successfully but the remote branch "
                f"does not point at the intended commit {target_sha}."
            )
    except RuntimeError as error:
        message = str(error)
        print("Push branch failed")
        print(message[-4000:])

        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "push_branch",
            "error": message,
        }

    history = complete_intent(
        intent,
        list(state.get("side_effect_history", [])),
        remote_sha=remote_sha,
        reconciled=False,
    )

    print(f"Pushed branch: {state['branch']}")
    return {
        "workflow_status": "publishing",
        "side_effect_intent": {},
        "side_effect_history": history,
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_push_branch(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "prepare_draft_pr"


def prepare_draft_pr_node(state: WorkflowState) -> dict:
    target_sha = state.get("final_commit_sha")

    if not target_sha:
        message = "Cannot prepare draft PR without a final commit SHA."
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "prepare_draft_pr",
            "error": message,
        }

    intent = prepare_draft_pr_intent(
        issue_number=state["issue_number"],
        branch=state["branch"],
        target_sha=target_sha,
    )
    print(f"Prepared remote operation: {intent['operation_id']}")
    return {
        "workflow_status": "publishing",
        "side_effect_intent": intent,
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_prepare_draft_pr(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "create_draft_pr"


def create_draft_pr_node(state: WorkflowState) -> dict:
    intent = state.get("side_effect_intent", {})

    if (
        intent.get("status") != "prepared"
        or intent.get("kind") != "draft_pr_upsert"
    ):
        message = "Draft PR upsert is missing a prepared side-effect intent."
        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "prepare_draft_pr",
            "error": message,
        }

    try:
        client = GitHubAppClient()

        title = (
            f"Implement #{state['issue_number']}: {state['issue_title']}"
        )

        completed_steps = "\n".join(
            f"- [x] {step['id']}: {step['title']}"
            for step in state["steps"]
            if step.get("status") == "completed"
        )

        body = (
            f"Closes #{state['issue_number']}\n\n"
            "## Summary\n\n"
            f"{state['plan'].get('summary', '')}\n\n"
            "## Completed steps\n\n"
            f"{completed_steps}\n\n"
            "## Validation\n\n"
            "- Local validation passed for every step\n"
            "- Automated reviewer approved every step\n"
            "- Final whole-plan validation passed\n"
            "- Final whole-plan review approved the integrated change\n\n"
            f"Final commit: `{state['final_commit_sha']}`\n\n"
            "<!-- investory-operation: "
            f"{intent['operation_id']} -->\n"
        )

        pull_request = client.find_open_pr_by_branch(
            state["branch"]
        )

        if pull_request is None:
            pull_request = client.create_draft_pr(
                title=title,
                body=body,
                head=state["branch"],
                base="main",
            )
            print(f"Created draft PR #{pull_request.number}")
        else:
            pull_request = client.update_pull_request(
                pull_request,
                title=title,
                body=body,
            )
            print(f"Updated PR #{pull_request.number}")

        print(pull_request.html_url)
    except RuntimeError as error:
        message = str(error)
        print("Draft PR creation failed")
        print(message[-4000:])

        return {
            "workflow_status": "blocked",
            "blocked_reason": message,
            "blocked_stage": "create_draft_pr",
            "error": message,
        }

    history = complete_intent(
        intent,
        list(state.get("side_effect_history", [])),
        pull_request_number=pull_request.number,
        pull_request_url=pull_request.html_url,
    )

    return {
        "workflow_status": "completed",
        "pull_request_number": pull_request.number,
        "pull_request_url": pull_request.html_url,
        "side_effect_intent": {},
        "side_effect_history": history,
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


def route_after_create_draft_pr(state: WorkflowState) -> str:
    if state["workflow_status"] == "blocked":
        return "blocked"

    return "cleanup"


def review_approved_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: REVIEW APPROVED")
    return {}


def review_changes_required_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: REVIEW CHANGES REQUIRED")

    for finding in state["review"].get(
        "findings",
        [],
    ):
        if finding.get("severity") == "blocking":
            print(
                f"- {finding.get('title')}: "
                f"{finding.get('recommendation')}"
            )

    return {}


def review_failure_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: REVIEW FAILURE")
    print(state["review_error"][-4000:])
    return {}


def blocked_node(state: WorkflowState) -> dict:
    print(
        f"RESULT: BLOCKED after "
        f"{state['attempt']} validation attempts"
    )

    blocked_reason = (
        state["blocked_reason"]
        or state["coder_error"]
        or state["review_error"]
        or state["test_output"]
        or state.get("final_review_error", "")
        or state.get("final_validation_output", "")
        or state["error"]
        or "Workflow blocked without a recorded reason."
    )

    print(blocked_reason[-4000:])

    return {
        "workflow_status": "blocked",
        "blocked_reason": blocked_reason,
        "blocked_stage": state.get("blocked_stage", ""),
    }


def build_graph():
    builder = StateGraph(WorkflowState)

    builder.add_node("load_issue", load_issue)
    builder.add_node("planner", planner_node)
    builder.add_node(
        "publish_plan",
        publish_plan_node,
    )
    builder.add_node(
        "planning_failure",
        planning_failure_node,
    )
    builder.add_node(
        "awaiting_user_input",
        awaiting_user_input_node,
    )
    builder.add_node(
        "prepare_workspace",
        prepare_workspace_node,
    )
    builder.add_node(
        "collect_repository_context",
        collect_repository_context_node,
    )
    builder.add_node(
        "start_environment",
        start_environment_node,
    )
    builder.add_node(
        "prepare_current_step",
        prepare_current_step_node,
    )
    builder.add_node("coder", coder_node)
    builder.add_node(
        "run_validation",
        run_validation_node,
    )
    builder.add_node(
        "isolate_validation_failure",
        isolate_validation_failure_node,
    )
    builder.add_node(
        "isolate_review_failure",
        isolate_review_failure_node,
    )
    builder.add_node(
        "environment_failure",
        environment_failure_node,
    )
    builder.add_node(
        "project_validation_failure",
        project_validation_failure_node,
    )
    builder.add_node(
        "validation_success",
        validation_success_node,
    )
    builder.add_node("reviewer", reviewer_node)
    builder.add_node(
        "publish_review",
        publish_review_node,
    )
    builder.add_node("complete_step", complete_step_node)
    builder.add_node(
        "prepare_final_review",
        prepare_final_review_node,
    )
    builder.add_node(
        "final_validation",
        final_validation_node,
    )
    builder.add_node(
        "final_integration_coder",
        final_integration_coder_node,
    )
    builder.add_node(
        "isolate_final_validation_failure",
        isolate_final_validation_failure_node,
    )
    builder.add_node(
        "final_reviewer",
        final_reviewer_node,
    )
    builder.add_node(
        "isolate_final_review_failure",
        isolate_final_review_failure_node,
    )
    builder.add_node(
        "finalize_history",
        finalize_history_node,
    )
    builder.add_node(
        "workflow_complete",
        workflow_complete_node,
    )
    builder.add_node("prepare_push_branch", prepare_push_branch_node)
    builder.add_node("push_branch", push_branch_node)
    builder.add_node("prepare_draft_pr", prepare_draft_pr_node)
    builder.add_node("create_draft_pr", create_draft_pr_node)
    builder.add_node("blocked", blocked_node)
    builder.add_node("cleanup", cleanup_node)

    builder.add_edge(START, "load_issue")
    builder.add_edge("load_issue", "prepare_workspace")
    builder.add_edge(
        "prepare_workspace",
        "collect_repository_context",
    )
    builder.add_edge("collect_repository_context", "planner")

    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "publish_plan": "publish_plan",
            "planning_failure": "planning_failure",
        },
    )

    builder.add_conditional_edges(
        "publish_plan",
        route_after_plan_publication,
        {
            "start_environment": "start_environment",
            "awaiting_user_input": "awaiting_user_input",
        },
    )

    builder.add_edge("planning_failure", END)
    builder.add_edge("awaiting_user_input", END)

    builder.add_conditional_edges(
        "start_environment",
        route_after_environment,
        {
            "prepare_current_step": "prepare_current_step",
            "environment_failure": "environment_failure",
        },
    )

    builder.add_edge("prepare_current_step", "coder")

    builder.add_conditional_edges(
        "coder",
        route_after_coder,
        {
            "run_validation": "run_validation",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "isolate_validation_failure",
        route_after_failed_attempt,
        {
            "coder": "coder",
            "blocked": "blocked",
        },
    )

    builder.add_conditional_edges(
        "run_validation",
        route_after_validation,
        {
            "reviewer": "reviewer",
            "isolate_validation_failure": (
                "isolate_validation_failure"
            ),
        },
    )

    builder.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {
            "publish_review": "publish_review",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "isolate_review_failure",
        route_after_failed_attempt,
        {
            "coder": "coder",
            "blocked": "blocked",
        },
    )

    builder.add_conditional_edges(
        "publish_review",
        route_after_review_publication,
        {
            "complete_step": "complete_step",
            "isolate_review_failure": (
                "isolate_review_failure"
            ),
        },
    )

    builder.add_conditional_edges(
        "complete_step",
        route_after_step_completion,
        {
            "prepare_current_step": "prepare_current_step",
            "prepare_final_review": "prepare_final_review",
        },
    )

    builder.add_conditional_edges(
        "prepare_final_review",
        route_after_prepare_final_review,
        {
            "final_validation": "final_validation",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "final_validation",
        route_after_final_validation,
        {
            "final_reviewer": "final_reviewer",
            "final_integration_coder": (
                "final_integration_coder"
            ),
            "isolate_final_validation_failure": (
                "isolate_final_validation_failure"
            ),
        },
    )
    builder.add_conditional_edges(
        "final_integration_coder",
        route_after_final_integration_coder,
        {
            "final_validation": "final_validation",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "isolate_final_validation_failure",
        route_after_final_failed_attempt,
        {
            "final_integration_coder": (
                "final_integration_coder"
            ),
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "final_reviewer",
        route_after_final_reviewer,
        {
            "finalize_history": "finalize_history",
            "final_integration_coder": (
                "final_integration_coder"
            ),
            "isolate_final_review_failure": (
                "isolate_final_review_failure"
            ),
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "isolate_final_review_failure",
        route_after_final_failed_attempt,
        {
            "final_integration_coder": (
                "final_integration_coder"
            ),
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "finalize_history",
        route_after_finalize_history,
        {
            "workflow_complete": "workflow_complete",
            "blocked": "blocked",
        },
    )

    builder.add_edge("environment_failure", "blocked")
    builder.add_edge("workflow_complete", "prepare_push_branch")
    builder.add_conditional_edges(
        "prepare_push_branch",
        route_after_prepare_push_branch,
        {
            "push_branch": "push_branch",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "push_branch",
        route_after_push_branch,
        {
            "prepare_draft_pr": "prepare_draft_pr",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "prepare_draft_pr",
        route_after_prepare_draft_pr,
        {
            "create_draft_pr": "create_draft_pr",
            "blocked": "blocked",
        },
    )
    builder.add_conditional_edges(
        "create_draft_pr",
        route_after_create_draft_pr,
        {
            "cleanup": "cleanup",
            "blocked": "blocked",
        },
    )
    builder.add_edge("blocked", "cleanup")
    builder.add_edge("cleanup", END)

    checkpoint_path = Path(
        os.getenv(
            "CHECKPOINT_DB",
            "/app/data/checkpoints.db",
        )
    )
    checkpoint_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(
        checkpoint_path,
        check_same_thread=False,
    )

    return builder.compile(
        checkpointer=SqliteSaver(connection)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a blocked workflow from its saved checkpoint.",
    )
    args = parser.parse_args()

    initial_state: WorkflowState = {
        "issue_number": args.issue,
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
        "max_attempts": int(
            os.getenv("MAX_ATTEMPTS", "3")
        ),
        "step_baseline_sha": "",
        "attempt_artifacts": [],
        "last_failed_patch_path": "",
        "final_baseline_sha": "",
        "final_attempt": 0,
        "max_final_attempts": int(
            os.getenv(
                "MAX_FINAL_ATTEMPTS",
                os.getenv("MAX_ATTEMPTS", "3"),
            )
        ),
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

    config = {
        "configurable": {
            "thread_id": f"investory-issue-{args.issue}"
        }
    }

    graph = build_graph()
    
    if args.resume:
        snapshot = graph.get_state(config)

        if not snapshot.values:
            raise RuntimeError(
                f"No checkpoint exists for issue #{args.issue}"
            )

        saved_state = dict(snapshot.values)
        workflow_status = saved_state.get("workflow_status")
        blocked_stage = saved_state.get("blocked_stage", "")
        resume_from = resolve_resume_from(saved_state)

        configured_max_attempts = int(
            os.getenv(
                "MAX_ATTEMPTS",
                str(saved_state["max_attempts"]),
            )
        )

        configured_max_final_attempts = int(
            os.getenv(
                "MAX_FINAL_ATTEMPTS",
                str(saved_state.get("max_final_attempts", 3)),
            )
        )

        if (
            blocked_stage == "coder"
            and saved_state["attempt"] >= configured_max_attempts
        ):
            raise RuntimeError(
                "Retry limit is exhausted. Increase MAX_ATTEMPTS "
                f"above {saved_state['attempt']} before resuming."
            )

        if (
            blocked_stage == "final_integration_coder"
            and saved_state.get("final_attempt", 0)
            >= configured_max_final_attempts
        ):
            raise RuntimeError(
                "Whole-plan repair limit is exhausted. Increase "
                "MAX_FINAL_ATTEMPTS above "
                f"{saved_state.get('final_attempt', 0)} before resuming."
            )

        resume_updates = {
            "workflow_status": "implementing",
            "max_attempts": configured_max_attempts,
            "max_final_attempts": configured_max_final_attempts,
            "blocked_reason": "",
            "blocked_stage": "",
            "coder_error": "",
            "review_error": "",
            "final_review_error": "",
            "error": "",
        }

        if blocked_stage == "awaiting_user_input":
            resume_updates.update(
                reload_issue_for_planning(args.issue)
            )

        graph.update_state(
            config,
            resume_updates,
            as_node=resume_from,
        )
        graph.invoke(None, config=config)
        return
    
    graph.invoke(initial_state, config=config)


if __name__ == "__main__":
    main()
