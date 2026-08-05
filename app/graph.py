from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

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

    print("Environment failure")

    return {
        "environment_ready": False,
        "environment_output": result["output"],
        "validation_status": "environment_failure",
        "validation_exit_code": result["exit_code"],
        "error": result["output"],
    }


def route_after_environment(state: WorkflowState) -> str:
    if state["environment_ready"]:
        return "run_validation"

    return "environment_failure"


def run_validation_node(state: WorkflowState) -> dict:
    print("Running project validation")

    result = run_validation(
        Path(state["workspace"]),
        state["issue_number"],
    )

    if result["success"]:
        print("Validation success")

        return {
            "validation_status": "validation_success",
            "validation_exit_code": 0,
            "test_output": result["output"],
            "tests_passed": True,
            "attempt": state["attempt"] + 1,
            "error": "",
        }

    print("Project validation failure")

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

    return "project_validation_failure"


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


def build_graph():
    builder = StateGraph(WorkflowState)

    builder.add_node("load_issue", load_issue)
    builder.add_node(
        "prepare_workspace",
        prepare_workspace_node,
    )
    builder.add_node(
        "start_environment",
        start_environment_node,
    )
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
    builder.add_node("cleanup", cleanup_node)

    builder.add_edge(START, "load_issue")
    builder.add_edge("load_issue", "prepare_workspace")
    builder.add_edge(
        "prepare_workspace",
        "start_environment",
    )

    builder.add_conditional_edges(
        "start_environment",
        route_after_environment,
        {
            "run_validation": "run_validation",
            "environment_failure": "environment_failure",
        },
    )

    builder.add_conditional_edges(
        "run_validation",
        route_after_validation,
        {
            "validation_success": "validation_success",
            "project_validation_failure": (
                "project_validation_failure"
            ),
        },
    )

    builder.add_edge("environment_failure", "cleanup")
    builder.add_edge("project_validation_failure", "cleanup")
    builder.add_edge("validation_success", "cleanup")
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
