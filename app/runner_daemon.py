from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.tasks import TaskStore


_STOP = False


def _stop_requested(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def _runner_id() -> str:
    configured = os.getenv("RUNNER_ID", "").strip()
    if configured:
        return configured
    path = Path(os.getenv(
        "RUNNER_ID_PATH", "~/.config/investory-orchestrator/runner-id"
    )).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if value:
        return value
    value = "runner-" + uuid.uuid4().hex[:16]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return value


def _capabilities() -> set[str]:
    values = {
        item.strip()
        for item in os.getenv("RUNNER_CAPABILITIES", "codex,git,build,review").split(",")
        if item.strip()
    }
    return values


def _version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return os.getenv("ORCHESTRATOR_BUILD_SHA", "unknown")


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({
        "timestamp": time.time(),
        "runner_id": fields.pop("runner_id", ""),
        "event": event,
        **fields,
    }, separators=(",", ":"), default=str), flush=True)


def _refresh_codex_health(store: TaskStore) -> None:
    from scripts.mac_ssh_entrypoint import _codex_quota_report, _ensure_node_on_path
    from app.task_scheduler import _record_codex_quota_status

    environment = os.environ.copy()
    try:
        _ensure_node_on_path(environment)
        authentication = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=15, check=False,
            env=environment,
        )
        if authentication.returncode != 0:
            raise RuntimeError("Codex authentication is unavailable on this runner")
        quota = _codex_quota_report(environment)
    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
        _record_codex_quota_status(store, None, error=str(error))
        _log("codex_health_unavailable", error_type=type(error).__name__)
        return
    _record_codex_quota_status(store, quota)
    _log(
        "codex_health_updated",
        status=store.get_service_status("codex_quota")["status"],
        remaining_percent=quota.get("remaining_percent"),
    )


def _start_worker(job: dict[str, Any], log_dir: Path) -> subprocess.Popen[bytes]:
    job_id = str(job["job_id"])
    attempt_id = str(job["current_attempt_id"])
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{job_id}-{attempt_id}-supervisor.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "app.runner_job_worker",
                "--job-id", job_id,
                "--attempt-id", attempt_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    _log(
        "job_worker_started", runner_id=job["runner_id"], task_id=job["task_id"],
        job_id=job_id, attempt_id=attempt_id, supervisor_pid=process.pid,
        log_path=str(log_path),
    )
    return process


def run_daemon(*, once: bool = False) -> None:
    global _STOP
    store = TaskStore(os.getenv("DATABASE_URL") or os.getenv("TASK_DB", "/app/data/tasks.db"))
    runner_id = _runner_id()
    capabilities = _capabilities()
    max_codex = int(os.getenv("RUNNER_MAX_CODEX_PROCESSES", "1"))
    max_builds = int(os.getenv("RUNNER_MAX_BUILDS", "1"))
    lease_seconds = max(10, int(os.getenv("RUNNER_JOB_LEASE_SECONDS", "90")))
    poll_seconds = max(2, int(os.getenv("RUNNER_POLL_SECONDS", "5")))
    heartbeat_seconds = max(5, int(os.getenv("RUNNER_HEARTBEAT_SECONDS", "20")))
    health_check_seconds = max(
        30, int(os.getenv("RUNNER_HEALTH_CHECK_SECONDS", "60"))
    )
    log_dir = Path(os.getenv(
        "RUNNER_LOG_DIR", "~/.investory-orchestrator/runs/logs"
    )).expanduser()
    version = _version()
    last_heartbeat = 0.0
    last_health_check: float | None = None
    _STOP = False
    store.register_runner(
        runner_id, capabilities=capabilities,
        max_codex_processes=max_codex, max_builds=max_builds, version=version,
    )
    _log(
        "runner_online", runner_id=runner_id, version=version,
        capabilities=sorted(capabilities), max_codex_processes=max_codex,
        max_builds=max_builds,
    )

    while not _STOP:
        now = time.monotonic()
        try:
            if now - last_heartbeat >= heartbeat_seconds:
                if not store.heartbeat_runner(runner_id):
                    store.register_runner(
                        runner_id, capabilities=capabilities,
                        max_codex_processes=max_codex, max_builds=max_builds,
                        version=version,
                    )
                expired = store.mark_expired_jobs_uncertain()
                for job_id in expired:
                    _log(
                        "job_lease_expired_uncertain", runner_id=runner_id,
                        job_id=job_id,
                        action="manual reconciliation required; job was not requeued",
                    )
                last_heartbeat = now

            if (
                last_health_check is None
                or now - last_health_check >= health_check_seconds
            ):
                store.set_service_status(
                    "codex_quota", "unknown",
                    "Checking Codex authentication and quota on the pull runner.",
                    {"mode": "postgres_pull", "runner_id": runner_id},
                )
                _refresh_codex_health(store)
                last_health_check = time.monotonic()

            quota = store.get_service_status("codex_quota")
            quota_status = quota["status"] if quota else "unknown"
            if quota_status in {"healthy", "throttled"}:
                codex_capacity = 1 if quota_status == "throttled" else max_codex
                store.register_runner(
                    runner_id, capabilities=capabilities,
                    max_codex_processes=codex_capacity, max_builds=max_builds,
                    version=version,
                )
                job = store.claim_job(runner_id, lease_seconds=lease_seconds)
            else:
                job = None
            if job is not None:
                try:
                    _start_worker(job, log_dir)
                except OSError as error:
                    store.complete_job(
                        job["job_id"], runner_id, job["current_attempt_id"],
                        error={
                            "category": "runner_spawn_failure",
                            "error_type": type(error).__name__,
                        },
                    )
                    _log(
                        "job_worker_spawn_failed", runner_id=runner_id,
                        task_id=job["task_id"], job_id=job["job_id"],
                        attempt_id=job["current_attempt_id"],
                        error_type=type(error).__name__,
                    )
            if once:
                store.heartbeat_runner(runner_id, status="offline")
                return
        except Exception as error:
            _log(
                "runner_iteration_failed", runner_id=runner_id,
                error_type=type(error).__name__,
            )
            if once:
                store.heartbeat_runner(runner_id, status="offline")
                raise
        if not _STOP:
            time.sleep(poll_seconds)

    store.heartbeat_runner(runner_id, status="offline")
    _log("runner_stopped", runner_id=runner_id)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PostgreSQL pull-based task runner")
    parser.add_argument("--once", action="store_true", help="run one polling iteration")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop_requested)
    signal.signal(signal.SIGINT, _stop_requested)
    try:
        run_daemon(once=args.once)
        return 0
    except Exception as error:
        print(f"Runner daemon failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
