from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.state import WorkflowState
from app.tasks import TaskStatus, TaskStore
from app.task_scheduler import (
    _print_task,
    _sync_task_result,
    _task_status_for_workflow,
    _track_task_state,
    reconcile_merged_task,
    run_queue,
)
from app.issue_validation import validate_issue_contract


GraphFactory = Callable[[], Any]
ResumeResolver = Callable[[dict], str]
IssueReloader = Callable[[int], dict]


def _close_graph(graph: Any) -> None:
    checkpointer = getattr(graph, "checkpointer", None)
    connection = getattr(checkpointer, "conn", None)
    close = getattr(connection, "close", None)

    if callable(close):
        close()


def build_initial_state(
    issue_number: int,
    task_id: str = "",
    *,
    source: str = "github_issue",
    title: str = "",
    body: str = "",
) -> WorkflowState:
    workflow_mode = os.getenv("WORKFLOW_MODE", "legacy").strip().lower()
    if workflow_mode not in {"legacy", "simplified"}:
        raise ValueError("WORKFLOW_MODE must be 'legacy' or 'simplified'")
    return {
        "task_id": task_id,
        "task_source": source,
        "ci_repair_requested": False,
        "issue_number": issue_number,
        "issue_title": title,
        "issue_body": body,
        "repository_context": "",
        "workflow_status": "new",
        # Persist the selected route in the checkpoint. Older checkpoints do
        # not contain this field and therefore continue on the legacy route.
        "workflow_mode": workflow_mode,
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
        "workspace_audit": {},
        "issue_baseline_sha": "",
        "remote_baseline_sha": "",
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
        "no_change_outcome": False,
        "environment_output": "",
        "environment_ready": False,
        "environment_started": False,
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
        "reviewer_backend": "",
        "reviewer_provider": "",
        "reviewer_model": "",
        "review_independence": "secondary_automated_review",
        "review_context_fresh": False,
        "review_read_only": False,
        "coder_summary": "",
        "coder_report": {},
        "coder_error": "",
        "coder_backend": "",
        "coder_provider": "",
        "coder_model": "",
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


def _record_activity_changes(
    store: TaskStore, task_id: str, previous: dict[str, Any], current: dict[str, Any]
) -> None:
    plan = current.get("plan") or {}
    if plan and plan != previous.get("plan"):
        summary = plan.get("summary") or "Implementation plan created."
        store.add_activity(
            task_id, actor="planner", event_type="plan_created",
            message=summary,
            metadata={"step_count": len(plan.get("steps", []))},
        )

    for field, actor, kind, label in (
        ("coder_summary", "coder", "implementation_update", "Implementation update"),
        ("coder_error", "coder", "agent_error", "Coder reported an error"),
    ):
        value = current.get(field, "")
        if value and value != previous.get(field, ""):
            store.add_activity(task_id, actor=actor, event_type=kind,
                               message=value if field == "coder_summary" else f"{label}: {value}")

    for prefix, label in (("validation", "Step validation"), ("final_validation", "Final validation")):
        status_key = f"{prefix}_status"
        value = current.get(status_key, "")
        if value and value != previous.get(status_key, "") and value != "not_started":
            message = f"{label}: {value.replace('_', ' ')}."
            store.add_activity(task_id, actor="validator", event_type="validation_result",
                               message=message, metadata={"status": value})

    for prefix, actor in (("review", "reviewer"), ("final_review", "reviewer")):
        status_key = f"{prefix}_status"
        value = current.get(status_key, "")
        if value and value != previous.get(status_key, "") and value not in {"not_started", ""}:
            review = current.get(prefix, {}) or {}
            summary = review.get("summary", "") if isinstance(review, dict) else ""
            findings = review.get("findings", []) if isinstance(review, dict) else []
            message = f"Review result: {value.replace('_', ' ')}."
            if summary:
                message += f" {summary}"
            if findings:
                finding_titles = [
                    f"{item.get('title', 'Finding')} ({item.get('severity', 'unspecified')})"
                    for item in findings[:8]
                    if isinstance(item, dict)
                ]
                if finding_titles:
                    message += " Findings: " + "; ".join(finding_titles)
            store.add_activity(task_id, actor=actor, event_type="review_result", message=message,
                               metadata={"status": value, "finding_count": len(findings)})

    pr_url = current.get("pull_request_url", "")
    if pr_url and pr_url != previous.get("pull_request_url", ""):
        pr_number = current.get("pull_request_number", "")
        store.add_activity(
            task_id, actor="orchestrator", event_type="draft_pr_ready",
            message=f"Draft pull request #{pr_number} sent for review: {pr_url}",
            metadata={"pull_request_number": pr_number, "pull_request_url": pr_url},
        )

    reason = current.get("blocked_reason", "")
    if reason and reason != previous.get("blocked_reason", ""):
        store.add_activity(task_id, actor="orchestrator", event_type="blocked",
                           message=reason, metadata={"stage": current.get("blocked_stage", "")})


def run_cli(
    *,
    build_graph: GraphFactory,
    resolve_resume_from: ResumeResolver,
    reload_issue_for_planning: IssueReloader,
    argv: list[str] | None = None,
) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int)
    parser.add_argument("--task-id")
    parser.add_argument("--ci-repair", action="store_true")
    parser.add_argument("--submit-issue", type=int)
    parser.add_argument("--submit-task", metavar="TITLE")
    parser.add_argument("--update-task", metavar="TASK_ID")
    parser.add_argument("--body", help="Task description or updated task body.")
    parser.add_argument("--status", metavar="TASK_ID")
    parser.add_argument(
        "--reconcile-merged-pr",
        metavar="TASK_ID",
        help="Verify a human-merged PR, close its linked issue, and complete the task.",
    )
    parser.add_argument("--list-tasks", action="store_true")
    parser.add_argument("--run-queue", action="store_true")
    parser.add_argument(
        "--resume-queue",
        action="store_true",
        help="Clear a Codex authentication/quota dispatch pause after repairing it.",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a blocked workflow from its saved checkpoint.",
    )
    args = parser.parse_args(argv)

    task_store = (
        TaskStore(
            os.getenv("DATABASE_URL")
            or os.getenv("TASK_DB", "/app/data/tasks.db")
        )
        if (
            args.submit_issue is not None
            or args.submit_task
            or args.update_task
            or args.status
            or args.reconcile_merged_pr
            or args.list_tasks
            or args.run_queue
            or args.resume_queue
            or args.task_id
        )
        else None
    )
    if args.submit_issue is not None:
        from app.github_client import GitHubAppClient

        repository = os.getenv("GITHUB_REPOSITORY", "spider-su/investory")
        repository_config = task_store.get_repository(repository)
        if repository_config and not repository_config["enabled"]:
            raise RuntimeError(f"Repository is disabled in configuration: {repository}")
        client = GitHubAppClient()
        issue = client.get_issue(args.submit_issue)
        validation = validate_issue_contract(
            issue.title or "",
            issue.body or "",
            tuple(getattr(label, "name", str(label)) for label in getattr(issue, "labels", ())),
        )
        if not validation.valid:
            errors = "\n".join(f"- {item}" for item in validation.errors)
            marker = "<!-- investory-orchestrator-intake-status -->"
            client.upsert_issue_comment(
                issue.number,
                f"{marker}\nIssue not queued: it does not yet meet the ready-to-develop "
                f"contract.\n\n{errors}\n\nNo workspace or Codex run was started.",
                marker=marker,
            )
            raise RuntimeError(
                f"Issue #{issue.number} does not meet the ready-to-develop contract: "
                + "; ".join(validation.errors)
            )
        task = task_store.create(
            issue_number=issue.number,
            title=issue.title,
            body=issue.body or "",
            source="github_issue",
            repository=repository,
            metadata={
                "base_branch": (
                    repository_config["base_branch"]
                    if repository_config
                    else os.getenv("BASE_BRANCH", "develop")
                )
            },
        )
        print(f"Queued task {task.task_id}: {task.title}")
        return
    if args.submit_task:
        task = task_store.create(
            title=args.submit_task,
            body=args.body or "",
            source="prompt",
        )
        print(f"Queued task {task.task_id}: {task.title}")
        return
    if args.update_task:
        if args.body is None:
            parser.error("--body is required with --update-task")
        task = task_store.get(args.update_task)
        if task is None:
            raise RuntimeError(f"Task not found: {args.update_task}")
        if task.source != "prompt" or task.status != TaskStatus.BLOCKED:
            raise RuntimeError(
                "Only a blocked prompt task can be updated here; update a "
                "GitHub issue in GitHub and resume it."
            )
        task = task_store.transition(
            task.task_id,
            task.status,
            body=args.body,
            blocked_reason="",
        )
        print(f"Updated task body: {task.task_id}")
        return
    if args.status:
        task = task_store.get(args.status)
        if task is None:
            raise RuntimeError(f"Task not found: {args.status}")
        _print_task(task)
        return
    if args.reconcile_merged_pr:
        task = reconcile_merged_task(task_store, args.reconcile_merged_pr)
        _print_task(task)
        return
    if args.list_tasks:
        for task in task_store.list():
            _print_task(task)
        return
    if args.resume_queue:
        task_store.set_service_status(
            "codex_queue", "ready", "Queue pause cleared by operator.",
            {"cleared_at": time.time()},
        )
        print("Codex queue pause cleared; dispatch will resume if the Mac runner is healthy.")
        return
    if args.run_queue:
        run_queue(task_store, once=args.once)
        return
    task_input = task_store.get(args.task_id) if args.task_id else None
    if args.task_id and task_input is None:
        raise RuntimeError(f"Task not found: {args.task_id}")
    if args.issue is None and task_input is None:
        parser.error("--issue or an existing --task-id is required")
    if args.issue is None and task_input and task_input.issue_number is not None:
        args.issue = task_input.issue_number
    if args.issue is None and task_input:
        args.issue = -max(1, int(task_input.task_id, 16))
    assert args.issue is not None

    config = config_for_issue(args.issue)
    graph = build_graph()

    def invoke_with_tracking(initial_state: dict | None) -> None:
        if not args.task_id:
            if initial_state is None:
                graph.invoke(None, config=config)
            else:
                graph.invoke(initial_state, config=config)
            return
        if initial_state is not None:
            _track_task_state(task_store, args.task_id, TaskStatus.PLANNING)
            stream = graph.stream(
                initial_state, config=config, stream_mode="values"
            )
        else:
            stream = graph.stream(None, config=config, stream_mode="values")
        previous_state = (
            dict(graph.get_state(config).values)
            if initial_state is None
            else {}
        )
        for state in stream:
            _record_activity_changes(task_store, args.task_id, previous_state, state)
            previous_state = dict(state)
            status = _task_status_for_workflow(state.get("workflow_status", ""))
            if status is not None:
                _track_task_state(
                    task_store,
                    args.task_id,
                    status,
                    phase={
                        "planning": "preparing",
                        "implementing": "implementing",
                        "validating": "validating",
                        "reviewing": "reviewing",
                        "publishing": "publishing",
                    }.get(state.get("workflow_status", "")),
                    workspace=state.get("workspace", ""),
                    branch=state.get("branch", ""),
                    implementation_attempts=(
                        sum(step.get("attempts", 0) for step in state.get("steps", []))
                        + state.get("attempt", 0)
                        + state.get("final_attempt", 0)
                    ),
                    validation_attempts=(
                        sum(step.get("attempts", 0) for step in state.get("steps", []))
                        + state.get("attempt", 0)
                        + state.get("final_attempt", 0)
                    ),
                )
        snapshot = graph.get_state(config)
        final_state = dict(snapshot.values)
        _sync_task_result(task_store, args.task_id, final_state)

    if not args.resume:
        try:
            invoke_with_tracking(
                build_initial_state(
                    args.issue,
                    args.task_id,
                    source=(task_input.source if task_input else "github_issue"),
                    title=(task_input.title if task_input else ""),
                    body=(task_input.body if task_input else ""),
                )
            )
        finally:
            _close_graph(graph)
        return

    try:
        snapshot = graph.get_state(config)

        if not snapshot.values:
            if task_input is not None:
                invoke_with_tracking(
                    build_initial_state(
                        args.issue,
                        args.task_id,
                        source=task_input.source,
                        title=task_input.title,
                        body=task_input.body,
                    )
                )
                return
            raise RuntimeError(f"No checkpoint exists for issue #{args.issue}")

        saved_state = dict(snapshot.values)
        if args.ci_repair:
            task = task_store.get(args.task_id or str(args.issue))
            if task is None or not (
                task.ci_status == "failed"
                or task.metadata.get("final_review_status") == "changes_required"
            ):
                raise RuntimeError("No CI or final-review repair is saved for this task.")
            details = task.metadata.get("ci_details", [])
            output = "\n".join(
                f"{item.get('name')}: {item.get('conclusion')} "
                f"{item.get('url')}\n{item.get('output', '')}"
                for item in details
                if task.ci_status == "failed"
                and item.get("conclusion") not in {"success", "skipped", "neutral"}
            )
            if task.metadata.get("final_review_status") == "changes_required":
                output = task.metadata.get("final_review_feedback", output)
            if not output:
                output = "CI checks failed; inspect the linked checks."
            graph.update_state(
                config,
                {
                    "workflow_status": "implementing",
                    "ci_repair_requested": True,
                    "final_attempt": 0,
                    "final_validation_status": "project_validation_failure",
                    "final_validation_output": output or "CI checks failed; inspect the linked checks.",
                    "final_review_status": "not_started",
                    "final_review": {},
                    "blocked_reason": "",
                    "blocked_stage": "",
                    "error": "",
                },
                as_node="prepare_final_review",
            )
            invoke_with_tracking(None)
            return
        if saved_state.get("workflow_status") == "completed":
            if args.task_id:
                _sync_task_result(task_store, args.task_id, saved_state)
            return
        if saved_state.get("workflow_status") != "blocked":
            if not snapshot.next:
                raise RuntimeError(
                    "Saved workflow has no pending node; inspect its state "
                    "before restarting."
                )
            next_nodes = set(snapshot.next)
            if next_nodes & {"coder", "final_integration_coder"}:
                from app.retry_isolation import workspace_has_changes

                workspace = Path(saved_state.get("workspace", ""))
                if workspace.exists() and workspace_has_changes(workspace):
                    raise RuntimeError(
                        "A coder was interrupted with workspace changes. "
                        "Preserve and inspect the diff before resuming."
                    )
            invoke_with_tracking(None)
            return
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
            if task_input is not None and task_input.source == "prompt":
                refreshed = task_store.get(task_input.task_id)
                resume_updates.update({
                    "issue_title": refreshed.title,
                    "issue_body": refreshed.body,
                    "workflow_status": "planning",
                    "plan": {},
                    "plan_markdown": "",
                    "plan_published": False,
                    "planning_error": "",
                    "requires_user_input": False,
                    "steps": [],
                    "current_step": 0,
                    "completed_steps": [],
                })
            else:
                resume_updates.update(
                    reload_issue_for_planning(args.issue)
                )

        graph.update_state(
            config,
            resume_updates,
            as_node=resume_from,
        )
        invoke_with_tracking(None)
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
