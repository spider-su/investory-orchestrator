from __future__ import annotations

import errno
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
from app.issue_validation import validate_issue_contract
from app.tasks import TaskStatus, TaskStore


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
                "release_promotion": {"status": "not_required"},
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
                "workspace_audit": workflow.get("workspace_audit", {}),
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
        print(
            "Task completed after authorized merge and successful post-merge CI. "
            "Review the release-promotion PR separately."
        )
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
    if not _ssh_target():
        store.set_service_status(
            "runner", "ready", "Local worker mode; remote Mac health check is disabled.",
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
        _log_event("runner_health", node="scheduler", status="unavailable", error=str(error))


def _codex_outage_category(reason: str) -> str | None:
    normalized = reason.casefold()
    auth_patterns = (
        "codex authentication unavailable", "not logged in", "login required",
        "authentication required", "not authenticated", "please login",
        "run codex login", "login expired", "unauthorized", "invalid api key", "token expired",
        "401 unauthorized", " 401",
    )
    quota_patterns = (
        "insufficient_quota", "usage limit", "usage cap", "quota exceeded", "rate limit",
        "rate_limit_exceeded",
        "too many requests", "plan limit", " 429", "http 429",
    )
    if any(pattern in normalized for pattern in auth_patterns):
        return "authentication"
    if any(pattern in normalized for pattern in quota_patterns):
        return "quota"
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
    codex_queue = store.get_service_status("codex_queue")
    if codex_queue and codex_queue["status"] == "paused":
        return codex_queue["detail"] or "Codex queue is paused."
    return ""


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


def run_queue(store: TaskStore, *, once: bool = False) -> None:
    """Run queued tasks, then poll CI for tasks whose worker has exited."""
    max_active = max(1, int(os.getenv("MAX_ACTIVE_TASKS", "3")))
    max_codex = max(1, int(os.getenv("MAX_CODEX_PROCESSES", "2")))
    max_builds = max(1, int(os.getenv("MAX_BUILDS", "1")))
    worker_limit = min(max_active, max_codex, max_builds)
    while True:
        _poll_ready_issues(store)
        _refresh_runner_health(store)
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
        retry_limit = max(0, int(os.getenv("CI_RETRY_ATTEMPTS", "3")))
        candidates.extend(
            task for task in store.list({TaskStatus.BLOCKED})
            if task.repository in repositories and (
                (
                    task.ci_status == "failed"
                    and task.ci_attempts <= retry_limit
                ) or (
                    task.metadata.get("final_review_status") == "changes_required"
                    and task.metadata.get("final_review_repairs", 0) <= retry_limit
                ) or task.metadata.get("recovery_pending", False)
            )
        )
        pause_reason = _dispatch_pause_reason(store)
        if pause_reason:
            candidates = []
            _log_event("dispatch_paused", reason=pause_reason)
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
                    validation = validate_issue_contract(
                        issue.title or "", issue.body or "", issue_labels
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
                    body=issue.body or "",
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
        promotion_status = (
            task.metadata.get("release_promotion", {}).get("status", "not_required")
            if task.status == TaskStatus.COMPLETED
            else ""
        )
        notification_status = (
            (
                f"{task.status.value}:no_changes"
                if task.metadata.get("completion", {}).get("outcome") == "no_changes"
                else f"{task.status.value}:{promotion_status}"
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
                "CI is green and the independent final review passed. Submit an "
                "approving GitHub review to authorize the orchestrator to merge "
                "this exact PR revision into the configured development branch."
            )
        elif task.status == TaskStatus.COMPLETED:
            completion = task.metadata.get("completion", {})
            promotion = task.metadata.get("release_promotion", {})
            promotion_status = promotion.get("status", "not_required")
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
            if promotion_status == "awaiting_review":
                body += (
                    f"\n\nRelease promotion PR: {promotion.get('pull_request_url')}\n"
                    "It promotes the accumulated development branch to the release "
                    "branch. Please review and merge when ready."
                )
            elif promotion_status == "pending":
                body += "\n\nPreparing the development-to-release promotion PR."
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
                    f"Release PR awaiting review: "
                    f"{task.metadata.get('release_promotion', {}).get('pull_request_url')}"
                    if task.metadata.get("release_promotion", {}).get("status") == "awaiting_review"
                    else "Preparing release promotion PR"
                    if task.metadata.get("release_promotion", {}).get("status") == "pending"
                    else "Completed with no changes; issue remains open"
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
    waiting = store.list({TaskStatus.WAITING_CI, TaskStatus.FINAL_REVIEW})
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
    _poll_approved_ready_tasks(store)
    _poll_merged_tasks(store)
    _ensure_release_promotion_prs(store)


def _poll_approved_ready_tasks(store: TaskStore) -> None:
    """Merge READY task PRs only after the configured user approves that exact head."""
    from app.github_client import GitHubAppClient

    release_branch = os.getenv("RELEASE_BRANCH", "main").strip() or "main"
    merge_method = os.getenv("APPROVED_PR_MERGE_METHOD", "squash").strip()
    for task in store.list({TaskStatus.READY}):
        base_branch = _task_base_branch(store, task)
        if base_branch.casefold() == release_branch.casefold():
            continue
        repository_config = store.get_repository(task.repository) or {}
        reviewer = str(repository_config.get("notification_login", "")).strip()
        if not reviewer or not task.pr_number:
            continue
        try:
            client = GitHubAppClient(task.repository)
            details = client.get_pull_request_details(task.pr_number)
            expected_head = task.metadata.get("final_review_head_sha", "")
            if (
                details["state"] != "open"
                or details["is_merged"]
                or details["is_draft"]
                or details["base_ref"] != base_branch
                or details["head_ref"] != task.branch
                or not expected_head
                or details["head_sha"] != expected_head
            ):
                continue
            approval = client.get_latest_review_approval(task.pr_number, reviewer)
            if (
                not approval
                or approval["state"] != "APPROVED"
                or approval["commit_sha"] != expected_head
                or approval["current_head_sha"] != expected_head
            ):
                continue
            ci_state, _ = client.get_pull_request_ci(task.pr_number)
            if ci_state != "success":
                continue
            approval = client.merge_approved_pull_request(
                task.pr_number,
                reviewer_login=reviewer,
                expected_head_sha=expected_head,
                merge_method=merge_method,
            )
            current = store.get(task.task_id)
            if current and current.status == TaskStatus.READY:
                store.transition(
                    current.task_id,
                    TaskStatus.READY,
                    metadata={**current.metadata, "approval_merge": approval},
                )
            print(
                f"Merged PR #{task.pr_number} after @{reviewer} approved "
                f"the reviewed head {expected_head}."
            )
        except (RuntimeError, ValueError, KeyError) as error:
            print(f"Unable to merge approved task {task.task_id}: {error}")


def _ensure_release_promotion_prs(store: TaskStore) -> None:
    """Open or reuse a develop-to-main PR after task merge and post-merge CI."""
    from app.github_client import GitHubAppClient

    release_branch = os.getenv("RELEASE_BRANCH", "main").strip() or "main"
    for task in store.list({TaskStatus.COMPLETED}):
        promotion = task.metadata.get("release_promotion", {})
        if promotion.get("status") != "pending":
            continue
        completion = task.metadata.get("completion", {})
        development_branch = str(completion.get("base_branch", "")).strip()
        if not development_branch or development_branch.casefold() == release_branch.casefold():
            continue
        try:
            client = GitHubAppClient(task.repository)
            pull_request = client.find_open_pr_by_branch(
                development_branch, base=release_branch
            )
            if pull_request is None:
                pull_request = client.create_release_promotion_pr(
                    head=development_branch,
                    base=release_branch,
                )
            issue_link = (
                f"https://github.com/{task.repository}/issues/{task.issue_number}"
                if task.issue_number is not None
                else task.repository
            )
            marker = "<!-- investory-orchestrator-release-promotion -->"
            client.upsert_issue_comment(
                pull_request.number,
                f"{marker}\nTask PR [#{task.pr_number}]({task.pr_url}) for "
                f"[{task.repository} issue #{task.issue_number}]({issue_link}) "
                f"merged into `{development_branch}` with successful post-merge CI. "
                f"This PR promotes accumulated `{development_branch}` changes to "
                f"`{release_branch}`; review and merge it manually when ready.",
                marker=marker,
            )
            current = store.get(task.task_id)
            if current and current.status == TaskStatus.COMPLETED:
                store.transition(
                    current.task_id,
                    TaskStatus.COMPLETED,
                    metadata={
                        **current.metadata,
                        "release_promotion": {
                            "status": "awaiting_review",
                            "pull_request_number": pull_request.number,
                            "pull_request_url": pull_request.html_url,
                            "head_branch": development_branch,
                            "base_branch": release_branch,
                        },
                    },
                )
        except (RuntimeError, ValueError, KeyError) as error:
            print(f"Unable to prepare release promotion for {task.task_id}: {error}")


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
        "release_promotion": (
            {"status": "pending", "base_branch": expected_base}
            if expected_base.casefold()
            != (os.getenv("RELEASE_BRANCH", "main").strip() or "main").casefold()
            else {"status": "not_required"}
        ),
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
        remote_review = bool(_ssh_target())
        verify_pr = remote_review or bool(os.getenv("GITHUB_APP_ID"))
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
        }
        if remote_review:
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
        attempts = metadata.get("final_review_attempts", 0) + 1
        retry_limit = max(0, int(os.getenv("MAX_FINAL_REVIEW_ATTEMPTS", "3")))
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
        },
    )
