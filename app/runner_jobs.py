from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def runner_jobs_dir() -> Path:
    configured = os.getenv("RUNNER_JOBS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    runs = Path(os.getenv("RUNS_DIR", "~/.investory-orchestrator/runs")).expanduser()
    return runs / "jobs"


class RunnerJobStore:
    """Small file-backed job ledger for the single local or Mac runner."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or runner_jobs_dir()).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _record_path(self, job_id: str) -> Path:
        self._validate_id(job_id)
        return self.directory / f"{job_id}.json"

    def _lock_path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.lock"

    @staticmethod
    def _validate_id(job_id: str) -> None:
        if len(job_id) != 24 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("job_id must be 24 lowercase hexadecimal characters")

    @contextmanager
    def _locked(self, job_id: str) -> Iterator[None]:
        with self._lock_path(job_id).open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, separators=(",", ":"), default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _read(self, job_id: str) -> dict[str, Any] | None:
        path = self._record_path(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def read_spec(self, job_id: str) -> dict[str, Any]:
        record = self._read(job_id)
        if record is None or not isinstance(record.get("spec"), dict):
            raise KeyError(f"Unknown runner job: {job_id}")
        return record["spec"]

    def submit(
        self,
        *,
        job_id: str,
        spec: dict[str, Any],
        command: list[str],
        environment: dict[str, str] | None = None,
        cwd: str | Path,
    ) -> dict[str, Any]:
        self._validate_id(job_id)
        if spec.get("job_id") != job_id or not spec.get("task_id"):
            raise ValueError("job spec must identify this job and its task")
        if spec.get("job_type") not in {"workflow", "final_review"}:
            raise ValueError("unsupported runner job type")
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("runner command must be a non-empty string array")
        working_directory = Path(cwd).expanduser().resolve()
        fingerprint = hashlib.sha256(json.dumps(
            {"spec": spec, "command": command, "cwd": str(working_directory)},
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        record_path = self._record_path(job_id)
        log_path = self.directory.parent / f"{job_id}.log"
        result_path = self.directory.parent / f"{job_id}.result.json"

        with self._locked(job_id):
            existing = self._read(job_id)
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    raise ValueError("job_id already exists with a different request")
                return self._public(existing)
            now = time.time()
            record = {
                "job_id": job_id,
                "task_id": spec["task_id"],
                "job_type": spec["job_type"],
                "repository": spec.get("repository", ""),
                "branch": spec.get("branch", ""),
                "expected_head_sha": spec.get("expected_head_sha", ""),
                "status": "submitted",
                "submitted_at": now,
                "started_at": None,
                "heartbeat_at": None,
                "finished_at": None,
                "worker_pid": None,
                "process_pid": None,
                "exit_code": None,
                "failure_classification": "",
                "result_path": str(result_path),
                "log_path": str(log_path),
                "cancel_requested": False,
                "fingerprint": fingerprint,
                "spec": spec,
                "command": command,
                "environment": environment or {},
                "cwd": str(working_directory),
            }
            self._atomic_json(record_path, record)
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "app.runner_jobs", "worker", job_id],
                    cwd=working_directory,
                    env={**os.environ, **(environment or {})},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
            record["worker_pid"] = process.pid
            self._atomic_json(record_path, record)
        return self._public(record)

    def get(self, job_id: str, *, include_result: bool = True) -> dict[str, Any]:
        self._validate_id(job_id)
        with self._locked(job_id):
            record = self._read(job_id)
            if record is None:
                return {"job_id": job_id, "status": "unknown"}
            if record.get("status") in {"submitted", "running"}:
                pid = record.get("worker_pid")
                if pid and not self._pid_alive(int(pid)):
                    record.update({
                        "status": "failed",
                        "finished_at": time.time(),
                        "failure_classification": "runner_process_lost",
                    })
                    self._atomic_json(self._record_path(job_id), record)
            public = self._public(record)
        if include_result and public["status"] in {"succeeded", "failed", "cancelled"}:
            result_path = Path(str(public.get("result_path", "")))
            if result_path.is_file():
                try:
                    public["result"] = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    public["result_error"] = "Runner result file could not be read."
        return public

    def cancel(self, job_id: str) -> dict[str, Any]:
        self._validate_id(job_id)
        with self._locked(job_id):
            record = self._read(job_id)
            if record is None:
                return {"job_id": job_id, "status": "unknown"}
            if record.get("status") in {"submitted", "running"}:
                record["cancel_requested"] = True
                self._atomic_json(self._record_path(job_id), record)
        return self.get(job_id, include_result=False)

    def run(self, job_id: str) -> int:
        self._validate_id(job_id)
        with self._locked(job_id):
            record = self._read(job_id)
            if record is None:
                return 2
            if record.get("status") != "submitted":
                return 0
            record.update({"status": "running", "started_at": time.time(),
                           "heartbeat_at": time.time(), "worker_pid": os.getpid()})
            self._atomic_json(self._record_path(job_id), record)

        child: subprocess.Popen[bytes] | None = None
        try:
            command = record["command"]
            env = {**os.environ, **record.get("environment", {})}
            with Path(record["log_path"]).open("ab") as log:
                child = subprocess.Popen(
                    command,
                    cwd=record["cwd"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
            with self._locked(job_id):
                record = self._read(job_id) or record
                record["process_pid"] = child.pid
                self._atomic_json(self._record_path(job_id), record)

            interval = max(2, int(os.getenv("RUNNER_HEARTBEAT_SECONDS", "20")))
            while child.poll() is None:
                time.sleep(interval)
                with self._locked(job_id):
                    record = self._read(job_id) or record
                    cancel_requested = bool(record.get("cancel_requested"))
                    if not cancel_requested:
                        record["heartbeat_at"] = time.time()
                        self._atomic_json(self._record_path(job_id), record)
                if cancel_requested and child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except OSError:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except OSError:
                            child.kill()
                        child.wait()
                    break
            exit_code = child.wait()
            result_path = Path(str(record.get("result_path", "")))
            with self._locked(job_id):
                record = self._read(job_id) or record
                cancelled = bool(record.get("cancel_requested"))
                status = "cancelled" if cancelled else "succeeded" if exit_code == 0 else "failed"
                record.update({
                    "status": status,
                    "finished_at": time.time(),
                    "heartbeat_at": time.time(),
                    "exit_code": exit_code,
                    "failure_classification": "" if exit_code == 0 else "execution_failure",
                })
                if result_path.is_file():
                    try:
                        record["result"] = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        record["result_error"] = "Runner result file could not be read."
                self._atomic_json(self._record_path(job_id), record)
            return exit_code
        except Exception as error:
            with self._locked(job_id):
                record = self._read(job_id) or record
                record.update({
                    "status": "failed",
                    "finished_at": time.time(),
                    "heartbeat_at": time.time(),
                    "exit_code": child.poll() if child is not None else 127,
                    "failure_classification": type(error).__name__,
                    "error": str(error),
                })
                self._atomic_json(self._record_path(job_id), record)
            return 1

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "job_id", "task_id", "job_type", "repository", "branch",
            "expected_head_sha", "status", "submitted_at", "started_at",
            "heartbeat_at", "finished_at", "worker_pid", "process_pid",
            "exit_code", "failure_classification", "result_path", "log_path",
            "cancel_requested", "result_error",
        )
        return {key: record[key] for key in keys if key in record}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != "worker":
        print("usage: python -m app.runner_jobs worker JOB_ID", file=sys.stderr)
        return 64
    return RunnerJobStore().run(arguments[1])


if __name__ == "__main__":
    raise SystemExit(main())
