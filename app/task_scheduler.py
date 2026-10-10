from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.agents.reviewer import (
    ReviewerError,
    review_classification,
    review_identity,
    review_implementation,
)
from app.issue_validation import format_issue_contract, validate_issue_contract
from app.tasks import JobKind, JobStatus, TaskPhase, TaskStatus, TaskStore


_last_repository_poll: dict[str, float] = {}
_last_runner_health_check = 0.0
_SCHEDULER_OWNER = (
    os.getenv("POD_UID")
    or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
)
_READY_STATUS_MARKER = "<!-- investory-orchestrator-intake-status -->"


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
    phase: str | None = None,
    **updates: Any,
) -> None:
    task = store.get(task_id)
    if task is None:
        return
    if status != TaskStatus.BLOCKED:
        updates.setdefault("blocked_reason", "")
        metadata = dict(updates.get("metadata", task.metadata))
        if "blocked_stage" in metadata:
            metadata.pop("blocked_stage", None)
            updates["metadata"] = metadata
    if phase in {item.value for item in TaskPhase}:
        metadata = dict(updates.get("metadata", task.metadata))
        metadata["phase"] = phase
        updates["metadata"] = metadata
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
        if workflow.get("no_change_outcome"):
            summary = str(
                workflow.get("final_review", {}).get("summary")
                or workflow.get("coder_summary")
                or "The approved workflow found no safe implementation changes."
            )
            metadata = {
                **task.metadata,
                "issue_number": workflow.get("issue_number"),
                "issue_title": workflow.get("issue_title", ""),
                "issue_body": workflow.get("issue_body", ""),
                "plan": workflow.get("plan", {}),
                "workspace_audit": workflow.get("workspace_audit", {}),
                "final_validation_status": workflow.get("final_validation_status", ""),
                "final_review_status": workflow.get("final_review_status", ""),
                "final_review": workflow.get("final_review", {}),
                "completed_steps": workflow.get("completed_steps", []),
                "attempt_artifacts": workflow.get("attempt_artifacts", []),
                "coder_report": workflow.get("coder_report", {}),
                "completion": {
                    "outcome": "no_changes",
                    "summary": summary,
                    "validation_status": workflow.get("final_validation_status", ""),
                    "review_status": workflow.get("final_review_status", ""),
                    "issue_closed": False,
                    "base_branch": task.metadata.get("base_branch", "develop"),
                },
            }
            _track_task_state(
                store,
                task_id,
                TaskStatus.COMPLETED,
                pr_number=None,
                pr_url="",
                ci_status="not_required",
                blocked_reason="",
                workspace=workflow.get("workspace", ""),
                branch=workflow.get("branch", ""),
                metadata=metadata,
            )
            return
        metadata = {
            **task.metadata,
            "issue_number": workflow.get("issue_number"),
            "issue_title": workflow.get("issue_title", ""),
            "issue_body": workflow.get("issue_body", ""),
            "plan": workflow.get("plan", {}),
            "issue_baseline_sha": workflow.get("issue_baseline_sha", ""),
            "workspace_audit": workflow.get("workspace_audit", {}),
            "final_validation_status": workflow.get("final_validation_status", ""),
            "final_validation_output": workflow.get("final_validation_output", ""),
            "final_validation_tree_sha": workflow.get("final_validation_tree_sha", ""),
            "final_review_status": workflow.get("final_review_status", ""),
            "final_review": workflow.get("final_review", {}),
            "final_review_tree_sha": workflow.get("final_review_tree_sha", ""),
            "final_review_head_sha": workflow.get("final_review_head_sha", ""),
            "final_review_clean_worktree": workflow.get("final_review_clean_worktree", False),
            "final_commit_sha": workflow.get("final_commit_sha", ""),
            "final_review_identity": {
                "backend": workflow.get("reviewer_backend", ""),
                "provider": workflow.get("reviewer_provider", ""),
                "model": workflow.get("reviewer_model", ""),
            },
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
                "workspace_audit": workflow.get("workspace_audit", {}),
                "coder_report": workflow.get("coder_report", {}),
                "coder_model": workflow.get("coder_model", ""),
                "coder_provider": workflow.get("coder_provider", ""),
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
        + (
            f"\t{task.blocked_reason}"
            if task.status == TaskStatus.BLOCKED and task.blocked_reason
            else ""
        )
    )
    if task.status in {TaskStatus.READY, TaskStatus.COMPLETED}:
        metadata = task.metadata
        plan = metadata.get("plan", {})
        review = metadata.get("final_review", {})
    if task.status == TaskStatus.READY:
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
    if task.status == TaskStatus.COMPLETED:
        completion = task.metadata.get("completion", {})
        if completion.get("outcome") == "no_changes":
            print("Outcome: no safe code changes were identified")
            print("PR: not created")
            print("Issue closed: False")
            print(f"Summary: {completion.get('summary', '')}")
            return
        print(f"PR: {task.pr_url}")
        print(f"Merge commit: {completion.get('merge_commit_sha', 'unknown')}")
        print(f"Merged by: {completion.get('merged_by', 'unknown')}")
        print(f"Issue closed: {completion.get('issue_closed', False)}")
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
        print("Task completed after human merge and successful post-merge CI.")
    elif task.status == TaskStatus.BLOCKED:
        print(f"Blocked: {task.blocked_reason}")
        details = task.metadata.get("ci_details", [])
        for item in details:
            if item.get("conclusion") not in {"success", "skipped", "neutral"}:
                print(
                    f"- {item.get('name')}: {item.get('conclusion')} "
                    f"{item.get('url')}"
                )


def _ssh_target() -> str:
    return os.getenv("MAC_SSH_TARGET", "").strip()


def _ssh_command(remote_args: list[str]) -> list[str]:
    target = _ssh_target()
    if not target or not re.fullmatch(r"[A-Za-z0-9_.@-]+", target):
        raise RuntimeError("MAC_SSH_TARGET must be an SSH user and host")
    identity = os.getenv("MAC_SSH_KEY_PATH", "/run/secrets/mac-runner-key")
    known_hosts = os.getenv("MAC_SSH_KNOWN_HOSTS", "/run/secrets/mac-known-hosts")
    return [
        "ssh", "-T", "-i", identity,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", f"UserKnownHostsFile={known_hosts}",
        target, shlex.join(remote_args),
    ]


def _remote_worker_command(task: Any) -> list[str]:
    issue_number = task.issue_number
    if issue_number is None and task.status == TaskStatus.BLOCKED:
        issue_number = -max(1, int(task.task_id, 16))
    args = ["run", task.task_id, str(issue_number or "-")]
    args.append(str(task.metadata.get("base_branch") or "-"))
    resume = task.status == TaskStatus.BLOCKED
    args.append("1" if resume else "0")
    ci_repair = resume and (
        task.ci_status == "failed"
        or task.metadata.get("final_review_status") == "changes_required"
    )
    args.append("1" if ci_repair else "0")
    args.append(os.getenv("ORCHESTRATOR_BUILD_SHA", "unknown"))
    return _ssh_command(args)


def _log_event(
    event: str,
    *,
    task_id: str = "",
    node: str = "scheduler",
    **fields: Any,
) -> None:
    print(json.dumps({
        "timestamp": time.time(),
        "task_id": task_id,
        "node": node,
        "event": event,
        **fields,
    }, separators=(",", ":"), default=str), flush=True)


def _refresh_runner_health(store: TaskStore, *, force: bool = False) -> None:
    global _last_runner_health_check
    now = time.monotonic()
    period = max(15, int(os.getenv("RUNNER_HEALTH_CHECK_SECONDS", "60")))
    if not force and now - _last_runner_health_check < period:
        return
    _last_runner_health_check = now
    if os.getenv("RUNNER_TRANSPORT", "ssh").strip().casefold() == "postgres_pull":
        epoch = time.time()
        stale_after = max(180, int(os.getenv("RUNNER_JOB_LEASE_SECONDS", "90")) * 3)
        required = {"codex", "git", "build", "review"}
        runners = [
            runner for runner in store.list_runners()
            if runner.get("status") == "online"
            and epoch - float(runner.get("last_heartbeat_at") or 0) <= stale_after
            and required.issubset(set(runner.get("capabilities") or []))
        ]
        ready = bool(runners)
        store.set_service_status(
            "runner", "ready" if ready else "unavailable",
            "PostgreSQL pull runner is online."
            if ready else "No fresh online runner has codex, git, build, and review capabilities.",
            {"mode": "postgres_pull", "runners": [
                {"runner_id": item["runner_id"], "version": item.get("version", ""),
                 "last_heartbeat_at": item.get("last_heartbeat_at")}
                for item in runners
            ]},
        )
        quota = store.get_service_status("codex_quota")
        if not quota or epoch - float(quota.get("updated_at") or 0) > stale_after:
            store.set_service_status(
                "codex_quota", "unknown",
                "Codex quota health is missing or stale; pull-runner dispatch is paused.",
                {"mode": "postgres_pull", "status": "stale"},
            )
        return
    if not _ssh_target():
        store.set_service_status(
            "runner", "ready", "Local worker mode; remote Mac health check is disabled.",
            {"mode": "local"},
        )
        store.set_service_status(
            "codex_quota", "unmonitored",
            "Quota monitoring is enabled for the remote Mac runner only.",
            {"mode": "local"},
        )
        return

    expected_sha = os.getenv("ORCHESTRATOR_BUILD_SHA", "unknown")
    try:
        result = subprocess.run(
            _ssh_command(["health", expected_sha]),
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        report = json.loads(result.stdout.strip().splitlines()[-1])
        ready = result.returncode == 0 and report.get("status") == "ready"
        store.set_service_status(
            "runner",
            "ready" if ready else "unavailable",
            report.get("detail", "Mac runner health checks failed."),
            report,
        )
        _record_codex_quota_status(store, report.get("codex_quota"))
        _log_event(
            "runner_health", node="scheduler",
            status="ready" if ready else "unavailable",
            checks=report.get("checks", {}),
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError, IndexError) as error:
        store.set_service_status(
            "runner", "unavailable", f"Mac health probe failed: {error}",
            {"mode": "mac_ssh"},
        )
        _record_codex_quota_status(store, None, error=str(error))
        _log_event("runner_health", node="scheduler", status="unavailable", error=str(error))


def _quota_thresholds() -> tuple[int, int]:
    throttle = int(os.getenv("CODEX_QUOTA_THROTTLE_REMAINING_PERCENT", "10"))
    pause = int(os.getenv("CODEX_QUOTA_PAUSE_REMAINING_PERCENT", "5"))
    if not 0 <= pause < throttle <= 100:
        raise ValueError("Codex quota thresholds must satisfy 0 <= pause < throttle <= 100")
    return throttle, pause


def _record_codex_quota_status(
    store: TaskStore,
    quota: Any,
    *,
    error: str = "",
) -> None:
    try:
        throttle, pause = _quota_thresholds()
    except ValueError as threshold_error:
        store.set_service_status(
            "codex_quota", "unknown", str(threshold_error), {"status": "invalid_config"},
        )
        return
    if not isinstance(quota, dict) or quota.get("status") != "available":
        detail = (
            str(quota.get("detail") or "Codex quota is unavailable; dispatch is paused.")
            if isinstance(quota, dict)
            else "Codex quota is unavailable; dispatch is paused."
        )
        metadata = {"status": "unavailable", "error": error}
        store.set_service_status("codex_quota", "unknown", detail, metadata)
        return
    remaining = quota.get("remaining_percent")
    if not isinstance(remaining, (int, float)):
        store.set_service_status(
            "codex_quota", "unknown", "Codex quota snapshot has no remaining percentage.", quota,
        )
        return
    remaining = max(0, min(100, int(remaining)))
    metadata = {
        **quota,
        "throttle_remaining_percent": throttle,
        "pause_remaining_percent": pause,
    }
    if remaining <= pause:
        status = "paused"
        detail = (
            f"Codex quota is {remaining}% remaining (pause at {pause}%); "
            "new task dispatch is paused."
        )
    elif remaining <= throttle:
        status = "throttled"
        detail = (
            f"Codex quota is {remaining}% remaining; dispatch is limited to one active task "
            f"at {throttle}% or less."
        )
    else:
        status = "healthy"
        detail = f"Codex quota is {remaining}% remaining; normal dispatch is enabled."
    store.set_service_status("codex_quota", status, detail, metadata)


def _codex_outage_category(reason: str) -> str | None:
    normalized = reason.casefold()
    quota_patterns = (
        "insufficient_quota", "usage limit", "usage cap", "quota exceeded", "rate limit",
        "rate_limit_exceeded",
        "too many requests", "plan limit", " 429", "http 429",
    )
    if any(pattern in normalized for pattern in quota_patterns):
        return "quota"
    auth_patterns = (
        "codex authentication unavailable", "not logged in", "login required",
        "authentication required", "not authenticated", "please login",
        "run codex login", "login expired", "unauthorized", "invalid api key", "token expired",
        "401 unauthorized", " 401",
    )
    if any(pattern in normalized for pattern in auth_patterns):
        return "authentication"
    return None


def _pause_on_codex_outage(store: TaskStore, task: Any) -> None:
    reason = task.blocked_reason or str(task.metadata.get("error", ""))
    category = _codex_outage_category(reason)
    if not category:
        return
    detail = (
        "Codex authentication failed. Repair the Mac Codex login, then run "
        "`python -m app --resume-queue`."
        if category == "authentication"
        else "Codex usage is rate limited or exhausted. Wait for quota recovery, "
        "then run `python -m app --resume-queue`."
    )
    store.set_service_status(
        "codex_queue", "paused", detail,
        {"category": category, "task_id": task.task_id},
    )
    _log_event("queue_paused", task_id=task.task_id, category=category)


def _dispatch_pause_reason(store: TaskStore) -> str:
    runner = store.get_service_status("runner")
    if runner and runner["status"] != "ready":
        return runner["detail"] or "Mac runner is unavailable."
    quota = store.get_service_status("codex_quota")
    if quota and quota["status"] == "unknown":
        return quota["detail"] or "Codex quota is unavailable; dispatch is paused."
    if quota and quota["status"] == "paused":
        return quota["detail"]
    _clear_recovered_quota_pause(store, quota)
    codex_queue = store.get_service_status("codex_queue")
    if codex_queue and codex_queue["status"] == "paused":
        return codex_queue["detail"] or "Codex queue is paused."
    return ""


def _clear_recovered_quota_pause(store: TaskStore, quota: Any) -> None:
    if not quota or quota["status"] not in {"healthy", "throttled"}:
        return
    paused = store.get_service_status("codex_queue")
    if not paused or paused["status"] != "paused":
        return
    metadata = paused.get("metadata") or {}
    category = metadata.get("category") if isinstance(metadata, dict) else None
    task = store.get(str(metadata.get("task_id", ""))) if isinstance(metadata, dict) else None
    original_category = _codex_outage_category(task.blocked_reason) if task else None
    if category != "quota" and not (category == "authentication" and original_category == "quota"):
        return
    store.set_service_status(
        "codex_queue", "ready",
        "Previous quota-triggered queue pause cleared after a fresh quota check.",
        {"category": "quota", "cleared_by": "fresh_quota_check"},
    )


def _remote_worker_is_running(task_id: str) -> bool:
    try:
        result = subprocess.run(
            _ssh_command(["probe", task_id]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode in {0, 1}:
            return result.returncode == 0
        print(
            f"Unable to determine whether Mac worker {task_id} is active "
            f"(SSH/probe exit {result.returncode}); holding its task claim."
        )
        return True
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        print(f"Unable to probe Mac worker {task_id}; holding its task claim.")
        return True


def _pull_job_spec(
    store: TaskStore, task: Any, *, kind: JobKind, sequence: int,
) -> dict[str, Any]:
    base_branch = _task_base_branch(store, task)
    task_branch = task.branch or (
        f"agent/issue-{task.issue_number}"
        if task.issue_number is not None
        else f"agent/task-{task.task_id}"
    )
    config_names = (
        "TARGET_ADAPTER", "AGENT_DEVCONTAINER_SCRIPT", "PLANNER_MODEL",
        "CODER_MODEL", "REVIEWER_MODEL", "MAX_ATTEMPTS",
        "MAX_FINAL_ATTEMPTS", "MAX_FINAL_REVIEW_ATTEMPTS", "MAX_REPAIRS",
        "WORKFLOW_MODE",
    )
    config_snapshot = {key: os.environ[key] for key in config_names if key in os.environ}
    config_snapshot["resume"] = kind == JobKind.REPAIR
    if kind == JobKind.REPAIR and (
        task.ci_status == "failed"
        or task.metadata.get("final_review_status") == "changes_required"
    ):
        config_snapshot["repair_kind"] = "ci"
    identity = f"{task.task_id}\0{sequence}\0{kind.value}".encode("utf-8")
    job_id = hashlib.sha256(identity).hexdigest()[:32]
    return {
        "job_id": job_id,
        "task_id": task.task_id,
        "kind": kind.value,
        "repository": task.repository,
        "base_branch": base_branch,
        "task_branch": task_branch,
        "base_sha": "",
        "expected_head_sha": str(task.metadata.get("final_review_head_sha") or ""),
        "prompt": f"{task.title}\n\n{task.body}".strip(),
        "config_snapshot": config_snapshot,
        "required_capabilities": ["build", "codex", "git", "review"],
    }


def _queue_pull_runner_job(
    store: TaskStore,
    task: Any,
    *,
    expected_status: TaskStatus,
    claimed_status: TaskStatus,
    kind: JobKind,
) -> dict[str, Any]:
    sequence = int(task.metadata.get("runner_dispatch_sequence", 0))
    spec = _pull_job_spec(store, task, kind=kind, sequence=sequence)
    job_id = spec["job_id"]
    task_metadata = dict(task.metadata)
    task_metadata.pop("recovery_pending", None)
    metadata = {
        **task_metadata,
        "worker_mode": "postgres_pull",
        "runner_job_id": job_id,
        "runner_job_kind": kind.value,
        "runner_job_sequence": sequence,
        "runner_job_spec": spec,
    }
    task = store.transition(
        task.task_id, claimed_status, expected=expected_status, metadata=metadata,
    )
    job = store.get_job(job_id)
    if job is None:
        job = store.enqueue_job(spec, job_id=job_id, priority=task.priority)
    return job


def _reconcile_pull_runner_jobs(store: TaskStore) -> None:
    active_statuses = {
        TaskStatus.PLANNING, TaskStatus.IMPLEMENTING, TaskStatus.VALIDATING,
        TaskStatus.REVIEWING, TaskStatus.PUBLISHING, TaskStatus.FINAL_REVIEW,
    }
    for task in store.list():
        if task.metadata.get("worker_mode") != "postgres_pull":
            continue
        job_id = task.metadata.get("runner_job_id")
        if not job_id:
            continue
        job = store.get_job(str(job_id))
        if job is None:
            result_detail = (
                "Reserved PostgreSQL runner job is missing from durable storage. "
                "The previous process state cannot be proven; inspect the runner "
                "and workspace before dispatching another attempt."
            )
            result_status = JobStatus.UNCERTAIN.value
            job = {"kind": task.metadata.get("runner_job_kind", "unknown")}
        else:
            result_status = str(job["status"])
            result_detail = ""
        if job.get("kind") == JobKind.REVIEW.value:
            continue
        if result_status in {JobStatus.PENDING.value, JobStatus.RUNNING.value}:
            continue
        metadata = {
            **task.metadata,
            **(
                {
                    "uncertain_runner_job": {
                        "job_id": str(job_id),
                        "kind": task.metadata.get("runner_job_kind", "unknown"),
                        "job_spec": task.metadata.get("runner_job_spec"),
                    },
                }
                if result_status == JobStatus.UNCERTAIN.value
                and store.get_job(str(job_id)) is None
                else {}
            ),
            "last_runner_job": {
                "job_id": job_id,
                "kind": job.get("kind", "unknown"),
                "status": result_status,
                "runner_id": job.get("runner_id"),
                "attempt_id": job.get("current_attempt_id"),
                "result": job.get("result"),
                "error": job.get("error"),
            },
            "runner_dispatch_sequence": int(
                task.metadata.get("runner_job_sequence", 0)
            ) + 1,
        }
        for key in ("runner_job_id", "runner_job_kind", "runner_job_sequence", "runner_job_spec"):
            metadata.pop(key, None)
        if result_status == JobStatus.UNCERTAIN.value:
            reason = result_detail or (
                "Runner lease expired; the previous process may still be active. "
                "Prove it stopped before safely requeuing this job."
            )
        elif result_status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            error = (job.get("error") or {}) if isinstance(job, dict) else {}
            reason = task.blocked_reason or (
                f"Pull runner job failed ({error.get('category', 'unknown')}); "
                "inspect the runner log before retrying."
            )
        elif result_status == JobStatus.SUCCEEDED.value and task.status in active_statuses:
            reason = (
                "Pull runner exited successfully without recording a completed "
                "workflow phase; inspect the saved checkpoint before retrying."
            )
        elif result_status == JobStatus.SUCCEEDED.value:
            store.transition(task.task_id, task.status, metadata=metadata)
            if task.status == TaskStatus.BLOCKED:
                current = store.get(task.task_id)
                if current is not None:
                    _pause_on_codex_outage(store, current)
            continue
        else:
            reason = f"Pull runner job finished with unrecognized status {result_status!r}."
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            store.transition(
                task.task_id, task.status,
                metadata={**metadata, "runner_job_warning": reason},
            )
            continue
        if task.status != TaskStatus.BLOCKED:
            store.transition(
                task.task_id, TaskStatus.BLOCKED, expected=task.status,
                blocked_reason=reason, metadata=metadata,
            )
        else:
            store.transition(
                task.task_id, TaskStatus.BLOCKED, expected=TaskStatus.BLOCKED,
                blocked_reason=reason, metadata=metadata,
            )
        if result_status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            current = store.get(task.task_id)
            if current is not None:
                _pause_on_codex_outage(store, current)


def _publish_pending_slack_activity(store: TaskStore) -> None:
    if not os.getenv("SLACK_BOT_TOKEN", "").strip() or not os.getenv(
        "SLACK_CHANNEL_ID", ""
    ).strip():
        return
    from app.slack_notifier import publish_task_activity

    for activity in store.list_pending_activity(limit=50):
        try:
            if publish_task_activity(activity):
                store.mark_activity_sent(activity["activity_id"])
        except Exception as error:
            print(f"Unable to deliver task activity to Slack ({type(error).__name__}).")
            return


def run_queue(store: TaskStore, *, once: bool = False) -> None:
    """Run queued tasks, then poll CI for tasks whose worker has exited."""
    max_active = max(1, int(os.getenv("MAX_ACTIVE_TASKS", "3")))
    max_codex = max(1, int(os.getenv("MAX_CODEX_PROCESSES", "2")))
    max_builds = max(1, int(os.getenv("MAX_BUILDS", "1")))
    worker_limit = min(max_active, max_codex, max_builds)
    while True:
        _publish_pending_slack_activity(store)
        _poll_ready_issues(store)
        _refresh_runner_health(store)
        pull_transport = (
            os.getenv("RUNNER_TRANSPORT", "ssh").strip().casefold()
            == "postgres_pull"
        )
        if pull_transport:
            _reconcile_pull_runner_jobs(store)
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
            if active.metadata.get("worker_mode") == "postgres_pull":
                job_id = active.metadata.get("runner_job_id")
                job = store.get_job(str(job_id)) if job_id else None
                if job and job["status"] in {
                    JobStatus.PENDING.value, JobStatus.RUNNING.value,
                }:
                    live_worker_count += 1
                continue
            pid = active.metadata.get("worker_pid")
            lease_owner = active.metadata.get("lease_owner")
            if _pid_alive(pid) and (not lease_owner or lease_owner == _SCHEDULER_OWNER):
                live_worker_count += 1
                continue
            if (
                active.metadata.get("worker_mode") == "mac_ssh"
                and _remote_worker_is_running(active.task_id)
            ):
                live_worker_count += 1
                continue
            store.transition(
                active.task_id,
                TaskStatus.BLOCKED,
                expected=active.status,
                blocked_reason="Worker stopped unexpectedly; recovering its saved checkpoint.",
                metadata={**active.metadata, "recovery_pending": True},
            )
            recovered = store.get(active.task_id)
            if recovered:
                _pause_on_codex_outage(store, recovered)
        repositories = {
            item["repository"]: item
            for item in store.list_repositories()
            if item["enabled"]
        }
        candidates = [
            task for task in store.list({TaskStatus.QUEUED})
            if task.repository in repositories
        ]
        retry_limit = min(3, max(0, int(os.getenv("CI_RETRY_ATTEMPTS", "3"))))
        candidates.extend(
            task for task in store.list({TaskStatus.BLOCKED})
            if task.repository in repositories and (
                (
                    task.ci_status == "failed"
                    and task.ci_attempts <= retry_limit
                ) or (
                    task.metadata.get("final_review_status") == "changes_required"
                    and task.metadata.get("final_review_repairs", 0) <= retry_limit
                    and task.implementation_attempts < 2
                ) or task.metadata.get("recovery_pending", False)
            )
        )
        pause_reason = _dispatch_pause_reason(store)
        if pause_reason:
            candidates = []
            _log_event("dispatch_paused", reason=pause_reason)
        quota_status = store.get_service_status("codex_quota")
        effective_worker_limit = worker_limit
        if quota_status and quota_status["status"] == "throttled":
            effective_worker_limit = min(effective_worker_limit, 1)
        for task in candidates:
            if len(running) + live_worker_count >= effective_worker_limit:
                break
            claimed_status = (
                TaskStatus.PLANNING
                if task.status == TaskStatus.QUEUED
                else TaskStatus.IMPLEMENTING
            )
            claimed_metadata = dict(task.metadata)
            claimed_metadata.pop("recovery_pending", None)
            claimed_metadata.pop("post_ci_review", None)
            claimed_metadata["phase"] = (
                "preparing" if task.status == TaskStatus.QUEUED else "repairing"
            )
            if pull_transport:
                kind = (
                    JobKind.IMPLEMENT
                    if task.status == TaskStatus.QUEUED
                    else JobKind.REPAIR
                )
                try:
                    job = _queue_pull_runner_job(
                        store, task, expected_status=task.status,
                        claimed_status=claimed_status, kind=kind,
                    )
                except (OSError, RuntimeError, ValueError, KeyError) as error:
                    latest = store.get(task.task_id)
                    if latest and latest.status == claimed_status:
                        store.transition(
                            task.task_id, TaskStatus.BLOCKED,
                            expected=claimed_status,
                            blocked_reason=f"Unable to enqueue PostgreSQL runner job: {error}",
                            metadata={**latest.metadata, "worker_mode": "postgres_pull"},
                        )
                    continue
                live_worker_count += 1
                _log_event(
                    "runner_job_enqueued", task_id=task.task_id,
                    job_id=job["job_id"], kind=job["kind"], status=job["status"],
                )
                continue
            try:
                store.transition(
                    task.task_id,
                    claimed_status,
                    expected=task.status,
                    metadata=claimed_metadata,
                )
            except RuntimeError:
                continue
            try:
                remote_target = _ssh_target()
                command = (
                    _remote_worker_command(task)
                    if remote_target
                    else [sys.executable, "-m", "app", "--task-id", task.task_id]
                )
                if not remote_target:
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
                worker_environment = os.environ.copy()
                if task.metadata.get("base_branch") and not remote_target:
                    worker_environment["BASE_BRANCH"] = task.metadata["base_branch"]
                process = subprocess.Popen(
                    command,
                    env=worker_environment,
                    text=True,
                )
            except (OSError, RuntimeError) as error:
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
                metadata={
                    **current.metadata,
                    "worker_pid": process.pid,
                    "worker_mode": "mac_ssh" if remote_target else "local",
                    "lease_owner": _SCHEDULER_OWNER,
                    "lease_started_at": time.time(),
                    "worker_heartbeat_at": time.time(),
                },
            )
            _log_event(
                "worker_started", task_id=task.task_id,
                worker_mode="mac_ssh" if remote_target else "local",
                pid=process.pid,
            )
            # At most one worker is started per scheduler iteration when the
            # build limit is one. The task remains durable if the process dies.
        for process, task_id in running:
            code = _wait_for_worker(store, process, task_id)
            current = store.get(task_id)
            if current and current.status == TaskStatus.BLOCKED:
                _pause_on_codex_outage(store, current)
            if code:
                current = store.get(task_id)
                remote_worker_active = bool(
                    current
                    and current.metadata.get("worker_mode") == "mac_ssh"
                    and _remote_worker_is_running(task_id)
                )
                if remote_worker_active:
                    _log_event(
                        "ssh_session_lost_worker_active", task_id=task_id,
                        exit_code=code,
                    )
                    continue
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
                _log_event("worker_exited", task_id=task_id, exit_code=code)
        _poll_ci(store)
        _notify_terminal_tasks(store)
        if once:
            return
        time.sleep(float(os.getenv("QUEUE_POLL_SECONDS", "30")))


def _wait_for_worker(
    store: TaskStore,
    process: subprocess.Popen[str],
    task_id: str,
) -> int:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return process.wait()
    interval = max(5, int(os.getenv("WORKER_HEARTBEAT_SECONDS", "20")))
    last_heartbeat = time.monotonic()
    while True:
        result = poll()
        if result is not None:
            return result
        now = time.monotonic()
        if now - last_heartbeat >= interval:
            store.heartbeat_worker(task_id, _SCHEDULER_OWNER, process.pid)
            _log_event("worker_heartbeat", task_id=task_id)
            last_heartbeat = now
        time.sleep(min(2, interval))


def _poll_ready_issues(
    store: TaskStore,
    *,
    now: float | None = None,
    force: bool = False,
) -> int:
    """Queue authorized issues from enabled repositories, once per configured interval."""
    current_time = time.monotonic() if now is None else now
    label = os.getenv("READY_ISSUE_LABEL", "ready_to_develop").strip()
    if not label:
        print("READY_ISSUE_LABEL is empty; issue intake is disabled.")
        return 0
    queued = 0
    for config in store.list_repositories():
        repository = config["repository"]
        if not config["enabled"]:
            continue
        interval = max(30, int(config.get("poll_interval_seconds", 60)))
        if not force and current_time - _last_repository_poll.get(repository, float("-inf")) < interval:
            continue
        # Record before the request so a failing GitHub API does not hot-loop.
        _last_repository_poll[repository] = current_time
        try:
            from app.github_client import GitHubAppClient

            client = GitHubAppClient(repository)
            issues = client.list_ready_issues(label)
        except (RuntimeError, ValueError, KeyError) as error:
            print(f"Unable to poll ready issues for {repository}: {error}")
            continue
        for issue in issues:
            try:
                existing = store.get(f"{repository}#{issue.number}")
                login = config.get("notification_login", "")
                mention = f"@{login} " if login else ""
                if existing is None:
                    issue_labels = tuple(
                        getattr(item, "name", str(item))
                        for item in getattr(issue, "labels", ())
                    )
                    original_body = issue.body or ""
                    issue_body = format_issue_contract(
                        issue.title or "", original_body, issue_labels
                    )
                    if issue_body != original_body:
                        client.update_issue_body(issue.number, issue_body)
                        client.upsert_issue_comment(
                            issue.number,
                            "<!-- investory-orchestrator-formatting -->\n"
                            f"{mention}I formatted this issue into the ready-to-develop "
                            "structure. The original description is preserved verbatim "
                            "at the bottom; formatting did not add product requirements.",
                            marker="<!-- investory-orchestrator-formatting -->",
                        )
                    validation = validate_issue_contract(
                        issue.title or "", issue_body, issue_labels
                    )
                    if not validation.valid:
                        details = "\n".join(f"- {error}" for error in validation.errors)
                        client.upsert_issue_comment(
                            issue.number,
                            f"{_READY_STATUS_MARKER}\n{mention}Issue not queued: it "
                            "does not yet meet the ready-to-develop contract.\n\n"
                            f"{details}\n\n"
                            f"The `{label}` label remains in place. Update the issue "
                            "and it will be checked again. No workspace or Codex run "
                            "was started.",
                            marker=_READY_STATUS_MARKER,
                        )
                        continue
                task = store.create(
                    issue_number=issue.number,
                    title=issue.title,
                    body=(
                        issue_body if existing is None
                        else issue.body or ""
                    ),
                    source="github_issue",
                    repository=repository,
                    metadata={"base_branch": config["base_branch"], "ready_label": label},
                )
                # Removing the authorization label is an acknowledgement. If it
                # fails, the durable task makes the next poll idempotent.
                client.upsert_issue_comment(
                    issue.number,
                    f"{_READY_STATUS_MARKER}\n{mention}Investory Orchestrator queued this issue. "
                    f"Current task status: **{task.status.value}**. Progress is visible in the orchestrator dashboard.",
                    marker=_READY_STATUS_MARKER,
                )
                client.remove_issue_label(issue.number, label)
                if existing is None and task.status == TaskStatus.QUEUED:
                    queued += 1
            except (RuntimeError, ValueError, KeyError) as error:
                print(f"Unable to acknowledge ready issue {repository}#{issue.number}: {error}")
    return queued


def _notify_terminal_tasks(store: TaskStore) -> None:
    repositories = {item["repository"]: item for item in store.list_repositories()}
    candidates = store.list({TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.COMPLETED})
    for task in candidates:
        if task.issue_number is None:
            continue
        repository_config = repositories.get(task.repository, {})
        login = repository_config.get("notification_login", "")
        if not login:
            continue
        notification_status = (
            (
                f"{task.status.value}:no_changes"
                if task.metadata.get("completion", {}).get("outcome") == "no_changes"
                else task.status.value
            )
            if task.status == TaskStatus.COMPLETED
            else task.status.value
        )
        already_notified = task.metadata.get("terminal_notification_status")
        if already_notified == notification_status:
            continue
        mention = f"@{login} "
        if task.status == TaskStatus.READY:
            body = (
                f"{mention}Investory Orchestrator is ready for human review.\n\n"
                f"PR: {task.pr_url}\n"
                "CI is green and the independent final review passed. Review and "
                "merge the pull request when you are satisfied. The orchestrator "
                "will record completion after it observes the merge and successful "
                "post-merge CI."
            )
        elif task.status == TaskStatus.COMPLETED:
            completion = task.metadata.get("completion", {})
            if completion.get("outcome") == "no_changes":
                body = (
                    f"{mention}Task completed with no code changes.\n\n"
                    f"{completion.get('summary', 'The approved workflow found no safe changes to make.')}\n\n"
                    "Final validation passed and the independent review approved the result. "
                    "No PR was created; the issue remains open for your review."
                )
            else:
                body = (
                    f"{mention}Task PR merged into `{completion.get('base_branch', 'develop')}` "
                    "and post-merge CI passed.\n\n"
                    f"Merged PR: {task.pr_url}\n"
                    f"Merge commit: `{completion.get('merge_commit_sha', 'unknown')}`"
                )
        else:
            body = (
                f"{mention}Investory Orchestrator is blocked and needs attention.\n\n"
                f"Reason: {task.blocked_reason or 'See task details.'}"
            )
        marker = "<!-- investory-orchestrator-terminal-notification -->"
        try:
            from app.github_client import GitHubAppClient

            client = GitHubAppClient(task.repository)
            if task.status == TaskStatus.READY and task.pr_number:
                client.mark_pull_request_ready(task.pr_number)
            client.upsert_issue_comment(
                task.issue_number,
                f"{marker}\n{body}",
                marker=marker,
            )
            status_detail = (
                f"PR ready for review: {task.pr_url}"
                if task.status == TaskStatus.READY
                else f"Blocked: {task.blocked_reason or 'See dashboard for details.'}"
                if task.status == TaskStatus.BLOCKED
                else (
                    "Completed with no changes; issue remains open"
                    if task.metadata.get("completion", {}).get("outcome") == "no_changes"
                    else f"Merged PR: {task.pr_url}"
                )
            )
            client.upsert_issue_comment(
                task.issue_number,
                f"{_READY_STATUS_MARKER}\n{mention}Task status: **{task.status.value}**. "
                f"{status_detail}",
                marker=_READY_STATUS_MARKER,
            )
            store.transition(
                task.task_id,
                task.status,
                metadata={
                    **task.metadata,
                    "terminal_notification_status": notification_status,
                },
            )
        except (RuntimeError, ValueError, KeyError) as error:
            print(f"Unable to notify for {task.task_id}: {error}")


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _poll_ci(store: TaskStore) -> None:
    retryable_reviews = [
        task for task in store.list({TaskStatus.BLOCKED})
        if task.metadata.get("final_review_retryable")
    ]
    for task in retryable_reviews:
        _run_final_review(store, task)
    waiting = [
        task for task in store.list({TaskStatus.WAITING_CI, TaskStatus.RUNNING})
        if task.status == TaskStatus.WAITING_CI
        or task.metadata.get("post_ci_review") is True
    ]
    for task in waiting:
        if task.status == TaskStatus.FINAL_REVIEW:
            _run_final_review(store, task)
            continue
        try:
            from app.github_client import GitHubAppClient

            client = GitHubAppClient(task.repository)
            state, details = client.get_pull_request_ci(task.pr_number)
        except RuntimeError as error:
            print(f"CI status unavailable for task {task.task_id}: {error}")
            continue
        if state == "success":
            store.add_activity(
                task.task_id, actor="orchestrator", event_type="ci_result",
                message=f"GitHub Actions passed for draft PR #{task.pr_number}.",
                metadata={"status": "success"},
            )
            updated = store.transition(
                task.task_id,
                TaskStatus.FINAL_REVIEW,
                ci_status="green",
                metadata={
                    **task.metadata,
                    "ci_details": details,
                    "phase": "reviewing",
                    "post_ci_review": True,
                },
            )
            _run_final_review(store, updated)
        elif state == "failure":
            store.add_activity(
                task.task_id, actor="orchestrator", event_type="ci_result",
                message=f"GitHub Actions failed for draft PR #{task.pr_number}; task is blocked for repair.",
                metadata={"status": "failure"},
            )
            store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                ci_status="failed",
                ci_attempts=task.ci_attempts + 1,
                blocked_reason="CI failed; repair requires --resume after inspecting the saved workflow.",
                metadata={**task.metadata, "ci_details": details, "phase": "repairing"},
            )
    _poll_merged_tasks(store)


def _task_base_branch(store: TaskStore, task: Any) -> str:
    configured = store.get_repository(task.repository)
    return str(
        task.metadata.get("base_branch")
        or (configured or {}).get("base_branch")
        or os.getenv("BASE_BRANCH", "develop")
    )


def _issue_linked(details: dict[str, Any], issue_number: int) -> bool:
    from app.github_client import GitHubAppClient

    return GitHubAppClient.pull_request_closes_issue(details, issue_number)


def _record_merged_task(
    store: TaskStore,
    task: Any,
    client: Any,
    details: dict[str, Any],
    checks: list[dict[str, str]],
    *,
    source: str,
    recorded_via: str,
    ci_state: str | None = None,
) -> None:
    if not details.get("is_merged"):
        raise RuntimeError(f"PR #{task.pr_number} has not been merged")
    if details.get("number") != task.pr_number:
        raise RuntimeError("GitHub returned a different pull request than the task records")
    expected_base = _task_base_branch(store, task)
    if details.get("base_ref") != expected_base:
        raise RuntimeError(
            f"PR #{task.pr_number} merged to {details.get('base_ref')!r}; "
            f"expected {expected_base!r}"
        )
    if task.branch and details.get("head_ref") != task.branch:
        raise RuntimeError(
            f"PR #{task.pr_number} head branch {details.get('head_ref')!r} "
            f"does not match task branch {task.branch!r}"
        )
    merge_sha = details.get("merge_commit_sha")
    merged_by = details.get("merged_by")
    merged_at = details.get("merged_at")
    if not merge_sha or not merged_by or not merged_at:
        raise RuntimeError("GitHub did not provide complete merge evidence")
    if task.issue_number is not None and not _issue_linked(details, task.issue_number):
        raise RuntimeError(
            f"PR #{task.pr_number} does not explicitly close issue #{task.issue_number}"
        )
    if ci_state is None:
        ci_state, merge_checks = client.get_commit_ci(merge_sha)
    else:
        merge_checks = checks
    if ci_state != "success":
        raise RuntimeError(
            f"Post-merge CI for {merge_sha} is {ci_state}; completion is not recorded"
        )
    issue_closed = False
    if task.issue_number is not None:
        client.close_issue(task.issue_number)
        issue_closed = True
    metadata = {
        **task.metadata,
        "completion": {
            "source": source,
            "recorded_via": recorded_via,
            "pr_number": task.pr_number,
            "merge_commit_sha": merge_sha,
            "merged_at": merged_at,
            "merged_by": merged_by,
            "base_branch": expected_base,
            "post_merge_ci_status": ci_state,
            "post_merge_ci_details": merge_checks,
            "issue_closed": issue_closed,
            **(
                {"approval_review": task.metadata["approval_merge"]}
                if task.metadata.get("approval_merge")
                else {}
            ),
        },
    }
    store.transition(
        task.task_id,
        TaskStatus.COMPLETED,
        expected=task.status,
        ci_status="green",
        blocked_reason="",
        metadata=metadata,
    )


def _poll_merged_tasks(store: TaskStore) -> None:
    from app.github_client import GitHubAppClient

    for task in store.list({TaskStatus.READY}):
        try:
            client = GitHubAppClient(task.repository)
            details = client.get_pull_request_details(task.pr_number)
            if details["is_merged"]:
                expected_head = task.metadata.get("final_review_head_sha")
                if expected_head and details["head_sha"] != expected_head:
                    raise RuntimeError(
                        "PR head changed after final review; task needs a new review"
                    )
                merge_state, merge_checks = client.get_commit_ci(
                    details.get("merge_commit_sha") or ""
                )
                if merge_state == "pending":
                    continue
                if merge_state != "success":
                    raise RuntimeError(
                        f"Post-merge CI is {merge_state} for "
                        f"{details.get('merge_commit_sha')}"
                    )
                _record_merged_task(
                    store, task, client, details, merge_checks,
                    source=(
                        "approved_review"
                        if task.metadata.get("approval_merge")
                        else "human_merge"
                    ),
                    recorded_via=(
                        "scheduler_approved_review"
                        if task.metadata.get("approval_merge")
                        else "scheduler_poll"
                    ),
                    ci_state=merge_state,
                )
            elif details["state"] == "closed":
                store.transition(
                    task.task_id,
                    TaskStatus.BLOCKED,
                    expected=TaskStatus.READY,
                    blocked_reason="PR was closed without merging.",
                )
        except (RuntimeError, ValueError, KeyError) as error:
            current = store.get(task.task_id)
            if current and current.status == TaskStatus.READY:
                store.transition(
                    task.task_id,
                    TaskStatus.BLOCKED,
                    expected=TaskStatus.READY,
                    blocked_reason=f"Unable to complete merged PR: {error}",
                    metadata={**current.metadata, "completion_retryable": True},
                )
            print(f"Merged PR status unavailable for task {task.task_id}: {error}")


def reconcile_merged_task(store: TaskStore, task_id: str) -> Any:
    task = store.get(task_id)
    if task is None:
        raise RuntimeError(f"Task not found: {task_id}")
    if task.status == TaskStatus.COMPLETED:
        return task
    if task.status not in {TaskStatus.BLOCKED, TaskStatus.READY}:
        raise RuntimeError(
            f"Only BLOCKED or READY tasks can be reconciled; task is {task.status.value}"
        )
    if not task.pr_number:
        raise RuntimeError("Task has no recorded pull request")
    from app.github_client import GitHubAppClient

    client = GitHubAppClient(task.repository)
    details = client.get_pull_request_details(task.pr_number)
    _record_merged_task(
        store,
        task,
        client,
        details,
        [],
        source="human_merge",
        recorded_via="manual_reconciliation",
    )
    completed = store.get(task_id)
    assert completed is not None
    return completed


def _reuse_published_review(store: TaskStore, task: Any, client: Any, head_sha: str) -> bool:
    """Reuse an independent approval only for the identical published candidate."""
    from app.agents.reviewer import review_classification

    metadata = task.metadata
    identity = metadata.get("final_review_identity") or {}
    independence = review_classification(
        metadata.get("coder_model", ""), identity.get("model", ""),
        coder_provider=metadata.get("coder_provider", ""),
        reviewer_provider=identity.get("provider", ""),
    )
    if not (
        head_sha and metadata.get("final_review_head_sha") == head_sha
        and metadata.get("final_commit_sha") == head_sha
        and metadata.get("final_review_tree_sha")
        and metadata.get("final_validation_tree_sha") == metadata.get("final_review_tree_sha")
        and metadata.get("final_review_clean_worktree") is True
        and metadata.get("final_review_status") == "approved"
        and metadata.get("final_review", {}).get("status") == "approved"
        and metadata.get("final_validation_status") == "validation_success"
        and independence == "independent"
        and task.pr_number and task.pr_url
    ):
        return False
    # Bind CI to the same SHA, even if the PR changed between polling and review.
    ci_state, details = client.get_commit_ci(head_sha)
    if ci_state != "success":
        store.transition(
            task.task_id,
            TaskStatus.BLOCKED if ci_state == "failure" else TaskStatus.WAITING_CI,
            ci_status="failed" if ci_state == "failure" else "pending",
            ci_attempts=task.ci_attempts + (ci_state == "failure"),
            blocked_reason="CI failed on the reviewed commit; repair required." if ci_state == "failure" else "",
            metadata={**metadata, "ci_details": details, "post_ci_review": False},
        )
        return True
    store.transition(
        task.task_id, TaskStatus.READY, ci_status="green", blocked_reason="",
        metadata={
            **metadata, "ci_details": details, "post_ci_review": False,
            "final_review_independence": independence,
            "review_reused_for_head": head_sha,
            "ready_gates": {
                "pull_request": True, "local_validation": True,
                "ci_green": True, "independent_review": True, "clean_worktree": True,
            },
        },
    )
    return True


def _run_final_review(store: TaskStore, task: Any) -> None:
    from pathlib import Path

    from app.agents.reviewer import (
        ReviewResult,
        review_classification,
        review_identity,
        review_implementation,
    )
    from app.github_client import GitHubAppClient

    metadata = task.metadata
    try:
        pull_review = (
            os.getenv("RUNNER_TRANSPORT", "ssh").strip().casefold()
            == "postgres_pull"
        )
        remote_review = not pull_review and bool(_ssh_target())
        verify_pr = pull_review or remote_review or bool(os.getenv("GITHUB_APP_ID"))
        if verify_pr:
            if task.pr_number is None:
                raise RuntimeError("Task has no pull request for final review")
            client = GitHubAppClient(task.repository)
            details = client.get_pull_request_details(task.pr_number)
            expected_base = _task_base_branch(store, task)
            if details["is_merged"] or details["state"] != "open":
                raise RuntimeError(
                    "PR is no longer open; use merged-PR reconciliation if it was merged"
                )
            if details["base_ref"] != expected_base:
                raise RuntimeError(
                    f"PR targets {details['base_ref']!r}; expected {expected_base!r}"
                )
            if details["head_ref"] != task.branch:
                raise RuntimeError("PR head branch does not match the task branch")
            if _reuse_published_review(store, task, client, details["head_sha"]):
                return
        else:
            details = {"head_sha": "", "head_ref": task.branch}
        if not task.workspace:
            raise RuntimeError("Task is missing its Mac workspace path")
        if verify_pr and not details["head_sha"]:
            raise RuntimeError("GitHub did not provide the PR head SHA")
        request = {
            "task_id": task.task_id,
            "workspace": task.workspace,
            "expected_branch": details["head_ref"],
            "expected_head_sha": details["head_sha"],
            "issue_number": int(metadata["issue_number"]),
            "issue_title": metadata["issue_title"],
            "issue_body": metadata.get("issue_body", ""),
            "plan": metadata["plan"],
            "validation_output": metadata.get("final_validation_output", ""),
            "baseline_sha": metadata.get("issue_baseline_sha"),
            "coder_report": metadata.get("coder_report"),
            "workspace_audit": metadata.get("workspace_audit"),
            "previous_review": metadata.get("final_review") if metadata.get("final_review_tree_sha") else None,
            "previous_review_tree_sha": metadata.get("final_review_tree_sha") or None,
        }
        if pull_review:
            review_job_id = metadata.get("final_review_job_id")
            review_spec = metadata.get("final_review_job_spec")
            if not review_job_id:
                sequence = int(metadata.get("final_review_job_sequence", 0))
                identity = (
                    f"{task.task_id}\0REVIEW\0{details['head_sha']}\0{sequence}"
                ).encode("utf-8")
                review_job_id = hashlib.sha256(identity).hexdigest()[:32]
                config_snapshot = {
                    key: os.environ[key]
                    for key in ("REVIEWER_MODEL", "MAX_FINAL_REVIEW_ATTEMPTS")
                    if key in os.environ
                }
                review_spec = {
                    "job_id": review_job_id,
                    "task_id": task.task_id,
                    "kind": JobKind.REVIEW.value,
                    "repository": task.repository,
                    "base_branch": expected_base,
                    "task_branch": task.branch,
                    "base_sha": str(request.get("baseline_sha") or ""),
                    "expected_head_sha": details["head_sha"],
                    "prompt": f"Review final PR head for task {task.task_id}",
                    "config_snapshot": config_snapshot,
                    "required_capabilities": ["codex", "git", "review"],
                    "review_request": request,
                }
                reserved = {
                    **metadata,
                    "final_review_job_id": review_job_id,
                    "final_review_job_spec": review_spec,
                    "final_review_job_head_sha": details["head_sha"],
                }
                store.transition(
                    task.task_id, TaskStatus.FINAL_REVIEW, expected=task.status,
                    metadata=reserved,
                )
                if store.get_job(review_job_id) is None:
                    store.enqueue_job(review_spec, job_id=review_job_id)
                return
            review_job = store.get_job(str(review_job_id))
            if review_job is None:
                metadata["uncertain_final_review_job"] = {
                    "job_id": str(review_job_id),
                    "job_spec": review_spec,
                    "head_sha": details["head_sha"],
                }
                raise RuntimeError(
                    "Reserved final-review runner job is missing; its execution "
                    "state cannot be proven, so it will not be dispatched again"
                )
            if review_job["status"] in {
                JobStatus.PENDING.value, JobStatus.RUNNING.value,
            }:
                return
            if review_job["status"] != JobStatus.SUCCEEDED.value:
                error = review_job.get("error") or {}
                raise RuntimeError(
                    "Final-review runner job "
                    f"{review_job['status']}: {error.get('category', 'unknown')}"
                )
            job_result = review_job.get("result") or {}
            reviewed = job_result.get("worker_result")
            if not isinstance(reviewed, dict):
                raise RuntimeError("Final-review runner returned no review result")
            if (
                reviewed.get("task_id") != task.task_id
                or reviewed.get("head_sha") != details["head_sha"]
                or reviewed.get("branch") != details["head_ref"]
            ):
                raise RuntimeError("Pull runner returned mismatched task or PR evidence")
            review_data = ReviewResult.model_validate(reviewed["review"]).model_dump(
                mode="json"
            )
            reviewer = reviewed["reviewer_identity"]
            if not isinstance(reviewer, dict) or not all(
                isinstance(reviewer.get(key), str)
                for key in ("backend", "provider", "model")
            ):
                raise RuntimeError("Pull runner returned an invalid reviewer identity")
            clean_worktree = reviewed.get("clean_worktree") is True
        elif remote_review:
            result = subprocess.run(
                _ssh_command(["review", task.task_id]),
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=int(os.getenv("MAC_REVIEW_TIMEOUT_SECONDS", "1800")),
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Mac final reviewer exited {result.returncode}: "
                    f"{result.stderr.strip()}"
                )
            reviewed = json.loads(result.stdout)
            if (
                reviewed.get("task_id") != task.task_id
                or reviewed.get("head_sha") != details["head_sha"]
                or reviewed.get("branch") != details["head_ref"]
            ):
                raise RuntimeError(
                    "Mac final reviewer returned mismatched task or PR evidence"
                )
            review_data = ReviewResult.model_validate(reviewed["review"]).model_dump(
                mode="json"
            )
            reviewer = reviewed["reviewer_identity"]
            if not isinstance(reviewer, dict) or not all(
                isinstance(reviewer.get(key), str)
                for key in ("backend", "provider", "model")
            ):
                raise RuntimeError("Mac final reviewer returned an invalid identity")
            clean_worktree = reviewed.get("clean_worktree") is True
        else:
            local_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=task.workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            local_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=task.workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            if verify_pr and (
                local_head != details["head_sha"] or local_branch != details["head_ref"]
            ):
                raise RuntimeError("Local workspace does not match the open PR head")
            if not verify_pr:
                details["head_sha"] = local_head
                details["head_ref"] = local_branch
            review = review_implementation(
                workspace=Path(task.workspace),
                issue_number=request["issue_number"],
                issue_title=request["issue_title"],
                issue_body=request["issue_body"],
                plan=request["plan"],
                validation_output=request["validation_output"],
                review_scope="whole_plan",
                baseline_sha=request["baseline_sha"] or None,
                coder_report=request["coder_report"],
                workspace_audit=request["workspace_audit"],
                previous_review=request["previous_review"],
                previous_review_tree_sha=request["previous_review_tree_sha"],
            )
            review_data = review.model_dump(mode="json")
            reviewer = review_identity()
            cleanliness = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=task.workspace,
                text=True,
                capture_output=True,
                check=True,
            )
            clean_worktree = not cleanliness.stdout.strip()
    except (
        RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        metadata = dict(metadata)
        if pull_review:
            metadata.pop("final_review_job_id", None)
            metadata.pop("final_review_job_spec", None)
            metadata.pop("final_review_job_head_sha", None)
            metadata["final_review_job_sequence"] = int(
                metadata.get("final_review_job_sequence", 0)
            ) + 1
        attempts = metadata.get("final_review_attempts", 0) + 1
        retry_limit = min(1, max(0, int(os.getenv("MAX_FINAL_REVIEW_ATTEMPTS", "1"))))
        retryable = attempts < retry_limit
        store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            blocked_reason=(
                f"Final PR review failed: {error}"
                if retryable
                else f"Final PR review failed after {attempts} attempts: {error}"
            ),
            metadata={
                **metadata,
                "final_review_attempts": attempts,
                "final_review_retryable": retryable,
                "final_review_error": str(error),
            },
        )
        return

    independence = review_classification(
        metadata.get("coder_model", ""),
        reviewer["model"],
        coder_provider=metadata.get("coder_provider", ""),
        reviewer_provider=reviewer["provider"],
    )
    updated = {
        **metadata,
        "final_review": review_data,
        "final_review_status": review_data["status"],
        "final_review_identity": reviewer,
        "final_review_independence": independence,
        "final_review_attempts": metadata.get("final_review_attempts", 0) + 1,
        "final_review_retryable": False,
        "final_review_head_sha": details["head_sha"],
    }
    if pull_review:
        updated.pop("final_review_job_id", None)
        updated.pop("final_review_job_spec", None)
        updated.pop("final_review_job_head_sha", None)
        updated["final_review_job_sequence"] = int(
            metadata.get("final_review_job_sequence", 0)
        ) + 1
    if review_data["status"] == "approved":
        missing_gates: list[str] = []
        if task.ci_status != "green":
            missing_gates.append("CI is not green")
        if task.pr_number is None or not task.pr_url:
            missing_gates.append("pull request is missing")
        if metadata.get("final_validation_status") != "validation_success":
            missing_gates.append("local validation is not recorded as passed")
        if not clean_worktree:
            missing_gates.append(
                "worktree is not clean or cleanliness could not be verified"
            )
        if independence != "independent":
            missing_gates.append("reviewer identity is not independent of the coder")
        if missing_gates:
            updated["ready_gates"] = {
                "pull_request": task.pr_number is not None and bool(task.pr_url),
                "local_validation": metadata.get("final_validation_status") == "validation_success",
                "ci_green": task.ci_status == "green",
                "independent_review": independence == "independent",
                "clean_worktree": clean_worktree,
            }
            store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                blocked_reason="READY gates not met: " + "; ".join(missing_gates),
                metadata=updated,
            )
            return
        updated["ready_gates"] = {
            "pull_request": True,
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
    feedback = json.dumps(review_data, indent=2)
    store.transition(
        task.task_id,
        TaskStatus.BLOCKED,
        blocked_reason="Final PR review requires changes.",
        metadata={
            **updated,
            "final_review_status": "changes_required",
            "final_review_feedback": feedback,
            "final_review_repairs": metadata.get("final_review_repairs", 0) + 1,
            "phase": "repairing",
        },
    )
