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
from app.state import WorkflowState
from app.test_runner import (
    run_validation,
    start_environment,
    stop_environment,
)
from app.workspace import prepare_workspace


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

    return {
        "plan": plan.model_dump(mode="json"),
        "plan_markdown": markdown,
        "plan_published": False,
        "planning_error": "",
        "requires_user_input": requires_user_input,
        "current_step": 0,
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

    return "prepare_workspace"


def planning_failure_node(state: WorkflowState) -> dict:
    print("RESULT: PLANNING FAILURE")
    print(state["planning_error"])
    return {}


def awaiting_user_input_node(
    state: WorkflowState,
) -> dict:
    print("RESULT: USER INPUT REQUIRED")

    for question in state["plan"].get(
        "open_questions",
        [],
    ):
        print(f"- {question}")

    return {}


def prepare_workspace_node(state: WorkflowState) -> dict:
    client = GitHubAppClient()

    workspace, branch = prepare_workspace(
        client,
        state["issue_number"],
    )

    print(f"Workspace prepared: {workspace}")
    print(f"Branch prepared: {branch}")

    return {
        "workspace": str(workspace),
        "branch": branch,
    }


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


def coder_node(state: WorkflowState) -> dict:
    next_attempt = state["attempt"] + 1

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
            validation_output=state["test_output"],
            attempt=next_attempt,
            max_attempts=state["max_attempts"],
        )
    except CoderError as error:
        message = str(error)
        print("Coder failed")
        print(message[-4000:])

        return {
            "coder_summary": "",
            "coder_error": message,
            "error": message,
        }

    print("Coder completed")

    return {
        "coder_summary": summary,
        "coder_error": "",
        "error": "",
    }


def route_after_environment(state: WorkflowState) -> str:
    if state["environment_ready"]:
        return "coder"

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
            "validation_status": "validation_success",
            "validation_exit_code": 0,
            "test_output": result["output"],
            "tests_passed": True,
            "attempt": state["attempt"] + 1,
            "error": "",
        }

    print("Project validation failed")

    return {
        "validation_status": "project_validation_failure",
        "validation_exit_code": result["exit_code"],
        "test_output": result["output"],
        "tests_passed": False,
        "attempt": state["attempt"] + 1,
        "error": "",
    }


def route_after_validation(state: WorkflowState) -> str:
    if state["validation_status"] == "validation_success":
        return "validation_success"

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
    return {}


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

    try:
        review = review_implementation(
            workspace=Path(state["workspace"]),
            issue_number=state["issue_number"],
            issue_title=state["issue_title"],
            issue_body=state["issue_body"],
            plan=state["plan"],
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
            "error": message,
        }

    markdown = review_to_markdown(review)

    print(f"Review result: {review.status}")

    return {
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

    return {"review_published": True}


def route_after_reviewer(
    state: WorkflowState,
) -> str:
    if state["review_status"] == "review_failure":
        return "review_failure"

    return "publish_review"


def route_after_review_publication(
    state: WorkflowState,
) -> str:
    if state["review_status"] == "approved":
        return "review_approved"

    return "review_changes_required"


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

    output = state["coder_error"] or state["test_output"]
    print(output[-4000:])

    return {}


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
        "start_environment",
        start_environment_node,
    )
    builder.add_node("coder", coder_node)
    builder.add_node(
        "run_validation",
        run_validation_node,
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
    builder.add_node(
        "review_approved",
        review_approved_node,
    )
    builder.add_node(
        "review_changes_required",
        review_changes_required_node,
    )
    builder.add_node(
        "review_failure",
        review_failure_node,
    )
    builder.add_node("blocked", blocked_node)
    builder.add_node("cleanup", cleanup_node)

    builder.add_edge(START, "load_issue")
    builder.add_edge("load_issue", "planner")

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
            "prepare_workspace": "prepare_workspace",
            "awaiting_user_input": "awaiting_user_input",
        },
    )

    builder.add_edge("planning_failure", END)
    builder.add_edge("awaiting_user_input", END)

    builder.add_edge(
        "prepare_workspace",
        "start_environment",
    )

    builder.add_conditional_edges(
        "start_environment",
        route_after_environment,
        {
            "coder": "coder",
            "environment_failure": "environment_failure",
        },
    )

    builder.add_conditional_edges(
        "coder",
        route_after_coder,
        {
            "run_validation": "run_validation",
            "blocked": "blocked",
        },
    )

    builder.add_conditional_edges(
        "run_validation",
        route_after_validation,
        {
            "validation_success": "validation_success",
            "coder": "coder",
            "blocked": "blocked",
        },
    )

    builder.add_edge(
        "validation_success",
        "reviewer",
    )
    builder.add_edge("review_approved", "cleanup")
    builder.add_edge(
        "review_changes_required",
        "cleanup",
    )
    builder.add_edge("review_failure", "cleanup")

    builder.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {
            "publish_review": "publish_review",
            "review_failure": "review_failure",
        },
    )

    builder.add_conditional_edges(
        "publish_review",
        route_after_review_publication,
        {
            "review_approved": "review_approved",
            "review_changes_required": (
                "review_changes_required"
            ),
        },
    )

    builder.add_edge("environment_failure", "cleanup")
    builder.add_edge("validation_success", "cleanup")
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
    args = parser.parse_args()

    initial_state: WorkflowState = {
        "issue_number": args.issue,
        "issue_title": "",
        "issue_body": "",
        "plan": {},
        "plan_markdown": "",
        "plan_published": False,
        "planning_error": "",
        "requires_user_input": False,
        "current_step": 0,
        "workspace": "",
        "branch": "",
        "attempt": 0,
        "max_attempts": int(
            os.getenv("MAX_ATTEMPTS", "3")
        ),
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
        "error": "",
    }

    config = {
        "configurable": {
            "thread_id": f"investory-issue-{args.issue}"
        }
    }

    graph = build_graph()
    graph.invoke(initial_state, config=config)


if __name__ == "__main__":
    main()
