from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from typing import Any

from app.state import WorkflowState


GraphFactory = Callable[[], Any]
ResumeResolver = Callable[[dict], str]
IssueReloader = Callable[[int], dict]


def _close_graph(graph: Any) -> None:
    checkpointer = getattr(graph, "checkpointer", None)
    connection = getattr(checkpointer, "conn", None)
    close = getattr(connection, "close", None)

    if callable(close):
        close()


def build_initial_state(issue_number: int) -> WorkflowState:
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
        "max_attempts": int(os.getenv("MAX_ATTEMPTS", "3")),
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
        "cleanup_status": "not_started",
        "cleanup_output": "",
        "cleanup_resume_stage": "",
        "cleanup_resume_reason": "",
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


def config_for_issue(issue_number: int) -> dict:
    return {
        "configurable": {
            "thread_id": f"investory-issue-{issue_number}"
        }
    }


def run_cli(
    *,
    build_graph: GraphFactory,
    resolve_resume_from: ResumeResolver,
    reload_issue_for_planning: IssueReloader,
    argv: list[str] | None = None,
) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a blocked workflow from its saved checkpoint.",
    )
    args = parser.parse_args(argv)

    config = config_for_issue(args.issue)
    graph = build_graph()

    if not args.resume:
        try:
            graph.invoke(
                build_initial_state(args.issue),
                config=config,
            )
        finally:
            _close_graph(graph)
        return

    try:
        snapshot = graph.get_state(config)

        if not snapshot.values:
            raise RuntimeError(
                f"No checkpoint exists for issue #{args.issue}"
            )

        saved_state = dict(snapshot.values)
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
    finally:
        _close_graph(graph)


def main(argv: list[str] | None = None) -> None:
    from app.graph import (
        build_graph,
        reload_issue_for_planning,
        resolve_resume_from,
    )

    run_cli(
        build_graph=build_graph,
        resolve_resume_from=resolve_resume_from,
        reload_issue_for_planning=reload_issue_for_planning,
        argv=argv,
    )
