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

    plan_dict = plan.model_dump(mode="json")
    steps = [
        {
            **step,
            "status": "pending",
            "attempts": 0,
            "commit_sha": "",
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


def prepare_current_step_node(state: WorkflowState) -> dict:
    index = state["current_step"]
    steps = [dict(step) for step in state["steps"]]

    if index >= len(steps):
        raise RuntimeError("Current step is outside the implementation plan")

    steps[index]["status"] = "in_progress"
    step = steps[index]

    print(
        f"Starting step {index + 1}/{len(steps)}: "
        f"{step['id']} — {step['title']}"
    )

    return {
        "workflow_status": "implementing",
        "steps": steps,
        "attempt": 0,
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
        )
    except CoderError as error:
        message = str(error)
        print("Coder failed")
        print(message[-4000:])

        return {
            "coder_summary": "",
            "coder_error": message,
            "blocked_reason": message,
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
        "error": "",
    }


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
        "error": "",
    }


def route_after_validation(state: WorkflowState) -> str:
    if state["validation_status"] == "validation_success":
        return "reviewer"

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

    return {"review_published": True}


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

    if state["attempt"] < state["max_attempts"]:
        return "coder"

    return "blocked"


def complete_step_node(state: WorkflowState) -> dict:
    index = state["current_step"]
    steps = [dict(step) for step in state["steps"]]
    step = steps[index]

    step["status"] = "completed"
    step["attempts"] = state["attempt"]

    print(f"Completed {step['id']}: {step['title']}")

    return {
        "steps": steps,
        "completed_steps": [
            *state["completed_steps"],
            step["id"],
        ],
        "current_step": index + 1,
        "attempt": 0,
    }


def route_after_step_completion(state: WorkflowState) -> str:
    if state["current_step"] >= len(state["steps"]):
        return "workflow_complete"

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
        "run_validation",
        route_after_validation,
        {
            "reviewer": "reviewer",
            "coder": "coder",
            "blocked": "blocked",
        },
    )

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
            "complete_step": "complete_step",
            "coder": "coder",
            "blocked": "blocked",
        },
    )

    builder.add_conditional_edges(
        "complete_step",
        route_after_step_completion,
        {
            "prepare_current_step": "prepare_current_step",
            "workflow_complete": "workflow_complete",
        },
    )

    builder.add_edge("environment_failure", "cleanup")
    builder.add_edge("workflow_complete", "cleanup")
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
        "commit_sha": "",
        "pull_request_number": 0,
        "pull_request_url": "",
        "ci_status": "not_started",
        "ci_run_id": 0,
        "ci_url": "",
        "ci_output": "",
        "blocked_reason": "",
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

        if workflow_status != "blocked":
            raise RuntimeError(
                "Only blocked workflows can be resumed. "
                f"Current status: {workflow_status}"
            )

        graph.update_state(
            config,
            {
                "workflow_status": "implementing",
                "attempt": 0,
                "blocked_reason": "",
                "coder_error": "",
                "review_error": "",
                "error": "",
            },
            as_node="prepare_current_step",
        )
        graph.invoke(None, config=config)
        return
    
    graph.invoke(initial_state, config=config)


if __name__ == "__main__":
    main()
