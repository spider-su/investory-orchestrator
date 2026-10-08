from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.tasks import JobKind, TaskStore


def _store() -> TaskStore:
    return TaskStore(os.getenv("DATABASE_URL") or os.getenv("TASK_DB", "/app/data/tasks.db"))


def _task_command(job: dict[str, Any]) -> list[str]:
    spec = job["job_spec"]
    task = _store().get(job["task_id"])
    if task is None:
        raise KeyError(f"Unknown task: {job['task_id']}")
    python = os.getenv("RUNNER_TASK_PYTHON", sys.executable)
    if job["kind"] == JobKind.REVIEW.value:
        return [
            python, "-m", "app.review_worker", "--job-id", job["job_id"],
            "--attempt-id", job["current_attempt_id"],
        ]
    if job["kind"] not in {JobKind.IMPLEMENT.value, JobKind.REPAIR.value}:
        raise ValueError(f"Unsupported runner job kind: {job['kind']}")
    command = [python, "-m", "app", "--task-id", task.task_id]
    if task.issue_number is not None:
        command.extend(["--issue", str(task.issue_number)])
    config = spec.get("config_snapshot", {})
    if job["kind"] == JobKind.REPAIR.value or config.get("resume") is True:
        command.append("--resume")
    if config.get("repair_kind") == "ci":
        command.append("--ci-repair")
    return command


def _result_path(job_id: str, attempt_id: str) -> Path:
    directory = Path(
        os.getenv("RUNNER_RESULT_DIR", "~/.investory-orchestrator/results")
    ).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{job_id}-{attempt_id}.json"


def _write_result(path: Path, result: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        json.dump(result, stream, separators=(",", ":"), default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()


def run_job(job_id: str, attempt_id: str) -> int:
    store = _store()
    job = store.get_job(job_id)
    if (
        not job
        or job["status"] != "running"
        or job["current_attempt_id"] != attempt_id
    ):
        raise RuntimeError("runner job attempt is no longer current")

    runner_id = str(job["runner_id"])
    lease_seconds = max(10, int(os.getenv("RUNNER_JOB_LEASE_SECONDS", "90")))
    heartbeat_seconds = max(2, int(os.getenv("RUNNER_HEARTBEAT_SECONDS", "20")))
    heartbeat_seconds = min(heartbeat_seconds, max(2, lease_seconds // 3))
    workspace_dir = Path(
        os.getenv("RUNNER_WORKSPACES_DIR", "~/.investory-orchestrator/task-workspaces")
    ).expanduser()
    runs_dir = Path(
        os.getenv("RUNNER_RUNS_DIR", "~/.investory-orchestrator/runs")
    ).expanduser()
    log_dir = Path(os.getenv("RUNNER_LOG_DIR", str(runs_dir / "logs"))).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{job_id}-{attempt_id}.log"
    result_path = _result_path(job_id, attempt_id)
    child_environment = os.environ.copy()
    child_environment.update({
        "RUNNER_WORKSPACES_DIR": str(workspace_dir),
        "WORKSPACES_DIR": str(workspace_dir),
        "RUNNER_RUNS_DIR": str(runs_dir),
        "RUNS_DIR": str(runs_dir),
        "RUNNER_JOB_RESULT_PATH": str(result_path),
        "GITHUB_REPOSITORY": str(job["repository"]),
        "BASE_BRANCH": str(job["base_branch"]),
    })
    # Only explicitly approved, non-secret settings are snapshotted into jobs.
    allowed_snapshot = {
        "TARGET_ADAPTER", "AGENT_DEVCONTAINER_SCRIPT", "PLANNER_MODEL",
        "CODER_MODEL", "REVIEWER_MODEL", "MAX_ATTEMPTS",
        "MAX_FINAL_ATTEMPTS", "MAX_FINAL_REVIEW_ATTEMPTS", "MAX_REPAIRS",
        "WORKFLOW_MODE",
    }
    for name in allowed_snapshot:
        value = job["config_snapshot"].get(name)
        if value is not None:
            child_environment[name] = str(value)

    started = time.monotonic()
    last_heartbeat = started
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            _task_command(job),
            cwd=Path(__file__).resolve().parents[1],
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
        if not store.record_job_process(
            job_id, runner_id, attempt_id, worker_pid=process.pid,
            log_path=str(log_path),
        ):
            _terminate(process)
            return 75
        while process.poll() is None:
            time.sleep(min(heartbeat_seconds, 2))
            if process.poll() is not None:
                break
            if time.monotonic() - last_heartbeat < heartbeat_seconds:
                continue
            try:
                renewed = store.heartbeat_job(
                    job_id, runner_id, attempt_id, lease_seconds=lease_seconds,
                )
            except Exception as error:
                print(
                    f"Job heartbeat failed ({type(error).__name__}); preserving the lease and retrying.",
                    file=sys.stderr, flush=True,
                )
                continue
            if not renewed:
                _terminate(process)
                return 75
            last_heartbeat = time.monotonic()
        exit_code = process.wait()

    result: dict[str, Any] = {
        "exit_code": exit_code,
        "duration_seconds": round(time.monotonic() - started, 3),
        "runner_id": runner_id,
        "attempt_id": attempt_id,
        "log_path": str(log_path),
    }
    if result_path.is_file():
        try:
            with result_path.open(encoding="utf-8") as stream:
                result["worker_result"] = json.load(stream)
        except (OSError, json.JSONDecodeError):
            result["worker_result_error"] = "Worker result artifact could not be read."
    else:
        task = store.get(job["task_id"])
        if task is not None:
            result["task_status"] = task.status.value
            result["branch"] = task.branch
            result["pull_request_number"] = task.pr_number
            result["pull_request_url"] = task.pr_url

    try:
        if exit_code == 0:
            completed = store.complete_job(
                job_id, runner_id, attempt_id, result=result,
            )
        else:
            completed = store.complete_job(
                job_id, runner_id, attempt_id,
                error={
                    "category": "worker_exit",
                    "exit_code": exit_code,
                    "log_path": str(log_path),
                },
            )
    except Exception as error:
        _write_result(result_path, {
            **result,
            "completion_error": f"{type(error).__name__}: {error}",
        })
        return 76
    if not completed:
        _write_result(result_path, {**result, "completion_error": "stale attempt was fenced"})
        return 77
    print(json.dumps({
        "event": "runner_job_finished",
        "job_id": job_id,
        "task_id": job["task_id"],
        "attempt_id": attempt_id,
        "exit_code": exit_code,
        "duration_seconds": result["duration_seconds"],
    }, separators=(",", ":")), flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    try:
        return run_job(args.job_id, args.attempt_id)
    except Exception as error:
        print(f"Runner job failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
