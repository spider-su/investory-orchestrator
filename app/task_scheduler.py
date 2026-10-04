from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.agents.reviewer import (
    ReviewerError,
    review_classification,
    review_identity,
    review_implementation,
)
from app.tasks import TaskStatus, TaskStore


def _task_status_for_workflow(workflow_status: str) -> TaskStatus | None:
    mapping = {
        "planning": TaskStatus.PLANNING,
        "implementing": TaskStatus.IMPLEMENTING,
        "validating": TaskStatus.VALIDATING,
        "reviewing": TaskStatus.REVIEWING,
        "publishing": TaskStatus.PUBLISHING,
        "blocked": TaskStatus.BLOCKED,
    }
    return mapping.get(workflow_status)


def _track_task_state(
    store: TaskStore,
    task_id: str,
    status: TaskStatus,
    **updates: Any,
) -> None:
    task = store.get(task_id)
    if task is None:
        return
    for counter in ("implementation_attempts", "validation_attempts", "ci_attempts"):
        if counter in updates:
            updates[counter] = max(getattr(task, counter), updates[counter])
    if task.status == status:
        if updates:
            store.transition(task_id, status, **updates)
        return
    try:
        store.transition(task_id, status, **updates)
    except ValueError:
        # Graph state can jump over a display-only stage when it resumes from
        # a saved checkpoint. Do not let the display aggregate alter execution.
        return


def _sync_task_result(
    store: TaskStore,
    task_id: str,
    workflow: dict[str, Any],
) -> None:
    task = store.get(task_id)
    if task is None:
        return
    if workflow.get("workflow_status") == "completed":
        metadata = {
            **task.metadata,
            "issue_number": workflow.get("issue_number"),
            "issue_title": workflow.get("issue_title", ""),
            "issue_body": workflow.get("issue_body", ""),
            "plan": workflow.get("plan", {}),
            "issue_baseline_sha": workflow.get("issue_baseline_sha", ""),
            "final_validation_status": workflow.get("final_validation_status", ""),
            "final_validation_output": workflow.get("final_validation_output", ""),
            "final_review_status": workflow.get("final_review_status", ""),
            "coder_model": workflow.get("coder_model", ""),
            "coder_provider": workflow.get("coder_provider", ""),
            "review_independence": workflow.get("review_independence", ""),
            "completed_steps": workflow.get("completed_steps", []),
            "attempt_artifacts": workflow.get("attempt_artifacts", []),
            "coder_report": workflow.get("coder_report", {}),
        }
        _track_task_state(
            store,
            task_id,
            TaskStatus.PUBLISHING,
            pr_number=workflow.get("pull_request_number") or None,
            pr_url=workflow.get("pull_request_url", ""),
            workspace=workflow.get("workspace", ""),
            branch=workflow.get("branch", ""),
            metadata=metadata,
            implementation_attempts=(
                workflow.get("attempt", 0)
                + workflow.get("final_attempt", 0)
            ),
            validation_attempts=(
                workflow.get("attempt", 0)
                + workflow.get("final_attempt", 0)
            ),
        )
        task = store.get(task_id)
        if task and task.status == TaskStatus.PUBLISHING:
            store.transition(
                task_id,
                TaskStatus.WAITING_CI,
                ci_status="queued",
            )
    elif workflow.get("workflow_status") == "blocked":
        _track_task_state(
            store,
            task_id,
            TaskStatus.BLOCKED,
            blocked_reason=workflow.get("blocked_reason", "Workflow blocked"),
            workspace=workflow.get("workspace", ""),
            branch=workflow.get("branch", ""),
            metadata={
                **task.metadata,
                "issue_number": workflow.get("issue_number"),
                "issue_title": workflow.get("issue_title", ""),
                "issue_body": workflow.get("issue_body", ""),
                "plan": workflow.get("plan", {}),
                "coder_report": workflow.get("coder_report", {}),
                "blocked_stage": workflow.get("blocked_stage", ""),
            },
        )
    elif workflow.get("planning_error") or workflow.get("error"):
        _track_task_state(
            store,
            task_id,
            TaskStatus.FAILED,
            blocked_reason=(
                workflow.get("planning_error") or workflow.get("error")
            ),
        )


def _print_task(task: Any) -> None:
    print(
        f"{task.task_id}\t{task.status.value}\t{task.title}\t"
        f"PR {task.pr_number or '-'}\tCI {task.ci_status}"
        + (f"\t{task.blocked_reason}" if task.blocked_reason else "")
    )
    if task.status == TaskStatus.READY:
        metadata = task.metadata
        plan = metadata.get("plan", {})
        review = metadata.get("final_review", {})
        print(f"PR: {task.pr_url}")
        print(
            f"CI: {task.ci_status} "
            f"({len(metadata.get('ci_details', []))} checks)"
        )
        print(
            "Review: "
            f"{metadata.get('final_review_status', 'unknown')} "
            f"({metadata.get('final_review_independence', 'unknown')})"
        )
        print(
            "Attempts: "
            f"implementation={task.implementation_attempts}, "
            f"CI repairs={task.ci_attempts}, "
            f"final-review repairs={metadata.get('final_review_repairs', 0)}"
        )
        print(f"Tests: {metadata.get('final_validation_status', 'unknown')}")
        validation_output = metadata.get("final_validation_output", "").strip()
        if validation_output:
            print(validation_output[-2000:])
        if plan.get("summary"):
            print(f"Summary: {plan['summary']}")
        steps = plan.get("steps", [])
        if steps:
            print("Implemented:")
            for step in steps:
                print(f"- {step.get('title', step.get('id', 'step'))}")
        findings = review.get("findings", [])
        if findings:
            print(f"Review findings: {len(findings)}")
            for finding in findings:
                print(
                    f"- {finding.get('severity')}: {finding.get('title')} — "
                    f"{finding.get('description')}"
                )
        remaining = metadata.get("coder_report", {}).get("remainingProblems", [])
        if remaining:
            print("Remaining risks:")
            for problem in remaining:
                print(f"- {problem}")
        print("Human action: review and merge the draft PR.")
    elif task.status == TaskStatus.BLOCKED:
        print(f"Blocked: {task.blocked_reason}")
        details = task.metadata.get("ci_details", [])
        for item in details:
            if item.get("conclusion") not in {"success", "skipped", "neutral"}:
                print(
                    f"- {item.get('name')}: {item.get('conclusion')} "
                    f"{item.get('url')}"
                )


def run_queue(store: TaskStore, *, once: bool = False) -> None:
    """Run queued tasks, then poll CI for tasks whose worker has exited."""
    max_active = max(1, int(os.getenv("MAX_ACTIVE_TASKS", "3")))
    max_codex = max(1, int(os.getenv("MAX_CODEX_PROCESSES", "2")))
    max_builds = max(1, int(os.getenv("MAX_BUILDS", "1")))
    worker_limit = min(max_active, max_codex, max_builds)
    while True:
        running: list[tuple[subprocess.Popen[str], str]] = []
        live_worker_count = 0
        active_statuses = {
            TaskStatus.PLANNING,
            TaskStatus.IMPLEMENTING,
            TaskStatus.VALIDATING,
            TaskStatus.REVIEWING,
            TaskStatus.PUBLISHING,
        }
        for active in store.list(active_statuses):
            pid = active.metadata.get("worker_pid")
            if _pid_alive(pid):
                live_worker_count += 1
                continue
            store.transition(
                active.task_id,
                TaskStatus.BLOCKED,
                expected=active.status,
                blocked_reason="Worker stopped unexpectedly; recovering its saved checkpoint.",
                metadata={**active.metadata, "recovery_pending": True},
            )
        candidates = store.list({TaskStatus.QUEUED})
        retry_limit = max(0, int(os.getenv("CI_RETRY_ATTEMPTS", "3")))
        candidates.extend(
            task for task in store.list({TaskStatus.BLOCKED})
            if (
                task.ci_status == "failed"
                and task.ci_attempts <= retry_limit
            ) or (
                task.metadata.get("final_review_status") == "changes_required"
                and task.metadata.get("final_review_repairs", 0) <= retry_limit
            )
            or (
                task.metadata.get("final_review_retryable", False)
                and task.metadata.get("final_review_attempts", 0) <= retry_limit
            )
            or task.metadata.get("recovery_pending", False)
        )
        for task in candidates:
            if len(running) + live_worker_count >= worker_limit:
                break
            claimed_status = (
                TaskStatus.PLANNING
                if task.status == TaskStatus.QUEUED
                else TaskStatus.IMPLEMENTING
            )
            claimed_metadata = dict(task.metadata)
            claimed_metadata.pop("recovery_pending", None)
            try:
                store.transition(
                    task.task_id,
                    claimed_status,
                    expected=task.status,
                    metadata=claimed_metadata,
                )
            except RuntimeError:
                continue
            command = [
                sys.executable,
                "-m",
                "app",
                "--task-id",
                task.task_id,
            ]
            if task.issue_number is not None:
                command.extend(["--issue", str(task.issue_number)])
            elif task.status == TaskStatus.BLOCKED:
                command.extend(["--issue", str(-max(1, int(task.task_id, 16)))])
            if task.status == TaskStatus.BLOCKED:
                command.append("--resume")
                if task.ci_status == "failed" or task.metadata.get(
                    "final_review_status"
                ) == "changes_required":
                    command.append("--ci-repair")
            try:
                process = subprocess.Popen(
                    command,
                    env=os.environ.copy(),
                    text=True,
                )
            except OSError as error:
                store.transition(
                    task.task_id,
                    TaskStatus.BLOCKED,
                    expected=claimed_status,
                    blocked_reason=f"Unable to start worker: {error}",
                )
                continue
            running.append((process, task.task_id))
            live_worker_count += 1
            current = store.get(task.task_id)
            store.transition(
                task.task_id,
                current.status,
                expected=current.status,
                metadata={**current.metadata, "worker_pid": process.pid},
            )
            # At most one worker is started per scheduler iteration when the
            # build limit is one. The task remains durable if the process dies.
        for process, task_id in running:
            code = process.wait()
            if code:
                current = store.get(task_id)
                if current and current.status not in {
                    TaskStatus.BLOCKED,
                    TaskStatus.FAILED,
                    TaskStatus.READY,
                    TaskStatus.WAITING_CI,
                }:
                    store.transition(
                        task_id,
                        TaskStatus.BLOCKED,
                        expected=current.status,
                        blocked_reason=(
                            f"Worker exited with status {code}; "
                            "inspect the saved workflow checkpoint."
                        ),
                        metadata={
                            **current.metadata,
                            "recovery_pending": False,
                        },
                    )
        _poll_ci(store)
        if once:
            return
        time.sleep(float(os.getenv("QUEUE_POLL_SECONDS", "30")))


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _poll_ci(store: TaskStore) -> None:
    waiting = store.list({TaskStatus.WAITING_CI, TaskStatus.FINAL_REVIEW})
    if not waiting:
        return
    from app.github_client import GitHubAppClient

    client = GitHubAppClient()
    for task in waiting:
        if task.status == TaskStatus.FINAL_REVIEW:
            _run_final_review(store, task)
            continue
        try:
            state, details = client.get_pull_request_ci(task.pr_number)
        except RuntimeError as error:
            print(f"CI status unavailable for task {task.task_id}: {error}")
            continue
        if state == "success":
            updated = store.transition(
                task.task_id,
                TaskStatus.FINAL_REVIEW,
                ci_status="green",
                metadata={**task.metadata, "ci_details": details},
            )
            _run_final_review(store, updated)
        elif state == "failure":
            store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                ci_status="failed",
                ci_attempts=task.ci_attempts + 1,
                blocked_reason="CI failed; repair requires --resume after inspecting the saved workflow.",
                metadata={**task.metadata, "ci_details": details},
            )


def _run_final_review(store: TaskStore, task: Any) -> None:
    from pathlib import Path

    from app.agents.reviewer import (
        ReviewerError,
        review_classification,
        review_identity,
        review_implementation,
    )

    metadata = task.metadata
    try:
        review = review_implementation(
            workspace=Path(task.workspace),
            issue_number=int(metadata["issue_number"]),
            issue_title=metadata["issue_title"],
            issue_body=metadata["issue_body"],
            plan=metadata["plan"],
            validation_output=metadata.get("final_validation_output", ""),
            review_scope="whole_plan",
            baseline_sha=metadata.get("issue_baseline_sha") or None,
        )
    except (ReviewerError, KeyError, OSError, ValueError) as error:
        attempts = metadata.get("final_review_attempts", 0) + 1
        store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            blocked_reason=f"Final PR review failed: {error}",
            metadata={
                **metadata,
                "final_review_attempts": attempts,
                "final_review_retryable": True,
                "final_review_error": str(error),
            },
        )
        return

    reviewer = review_identity()
    independence = review_classification(
        metadata.get("coder_model", ""),
        reviewer["model"],
        coder_provider=metadata.get("coder_provider", ""),
        reviewer_provider=reviewer["provider"],
    )
    updated = {
        **metadata,
        "final_review": review.model_dump(mode="json"),
        "final_review_status": review.status,
        "final_review_identity": reviewer,
        "final_review_independence": independence,
        "final_review_attempts": metadata.get("final_review_attempts", 0) + 1,
        "final_review_retryable": False,
    }
    if review.status == "approved":
        missing_gates: list[str] = []
        if task.ci_status != "green":
            missing_gates.append("CI is not green")
        if task.pr_number is None or not task.pr_url:
            missing_gates.append("draft PR is missing")
        if metadata.get("final_validation_status") != "validation_success":
            missing_gates.append("local validation is not recorded as passed")
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=task.workspace,
                text=True,
                capture_output=True,
                check=True,
            )
            if result.stdout.strip():
                missing_gates.append("worktree is not clean")
        except (OSError, subprocess.CalledProcessError):
            missing_gates.append("worktree cleanliness could not be verified")
        if independence != "independent":
            missing_gates.append("reviewer identity is not independent of the coder")
        if missing_gates:
            updated["ready_gates"] = {
                "draft_pr": task.pr_number is not None and bool(task.pr_url),
                "local_validation": metadata.get("final_validation_status") == "validation_success",
                "ci_green": task.ci_status == "green",
                "independent_review": independence == "independent",
                "clean_worktree": "worktree is not clean" not in missing_gates
                and "worktree cleanliness could not be verified" not in missing_gates,
            }
            store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                blocked_reason="READY gates not met: " + "; ".join(missing_gates),
                metadata=updated,
            )
            return
        updated["ready_gates"] = {
            "draft_pr": True,
            "local_validation": True,
            "ci_green": True,
            "independent_review": True,
            "clean_worktree": True,
        }
        store.transition(
            task.task_id,
            TaskStatus.READY,
            ci_status="green",
            blocked_reason="",
            metadata=updated,
        )
        return
    feedback = review.model_dump_json(indent=2)
    store.transition(
        task.task_id,
        TaskStatus.BLOCKED,
        blocked_reason="Final PR review requires changes.",
        metadata={
            **updated,
            "final_review_status": "changes_required",
            "final_review_feedback": feedback,
            "final_review_repairs": metadata.get("final_review_repairs", 0) + 1,
        },
    )
