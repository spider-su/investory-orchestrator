from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.runner_daemon import run_daemon
from app.tasks import JobStatus, TaskStore


class RunnerDaemonTests(unittest.TestCase):
    def test_launch_script_works_when_started_outside_repository(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            output = root / "runner-invocation.txt"
            python = root / "fake-python"
            python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n%s\\n' \"$PWD\" \"$*\" "
                "> \"$RUNNER_SCRIPT_TEST_OUTPUT\"\n",
                encoding="utf-8",
            )
            python.chmod(0o700)
            environment_file = root / "runner.env"
            environment_file.write_text(
                f"RUNNER_PYTHON={python}\n"
                f"RUNNER_SCRIPT_TEST_OUTPUT={output}\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["sh", str(repository / "scripts/runner-daemon.sh")],
                cwd=outside,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "RUNNER_ENV_FILE": str(environment_file),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            invoked_from, arguments = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(Path(invoked_from), repository)
            self.assertEqual(arguments, "-m app.runner_daemon")

    def test_restart_does_not_claim_a_job_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = str(root / "tasks.db")
            store = TaskStore(database)
            task = store.create(
                source="direct_prompt", title="Runner restart", body="Criteria",
                repository="owner/repository",
            )
            spec = {
                "task_id": task.task_id,
                "kind": "IMPLEMENT",
                "repository": "owner/repository",
                "base_branch": "develop",
                "task_branch": "agent/runner-restart",
                "base_sha": "a" * 40,
                "expected_head_sha": "",
                "prompt": "Implement and validate.",
                "config_snapshot": {},
                "required_capabilities": ["codex", "git", "build"],
            }
            job = store.enqueue_job(spec, job_id=uuid.uuid4().hex)
            environment = {
                "DATABASE_URL": "",
                "TASK_DB": database,
                "RUNNER_ID": "restart-test-runner",
                "RUNNER_ID_PATH": str(root / "runner-id"),
                "RUNNER_LOG_DIR": str(root / "logs"),
                "RUNNER_WORKSPACES_DIR": str(root / "workspaces"),
                "RUNNER_RUNS_DIR": str(root / "runs"),
                "RUNNER_RESULT_DIR": str(root / "results"),
                "RUNNER_HEALTH_CHECK_SECONDS": "3600",
            }
            start_calls: list[str] = []

            def start_worker(claimed: dict, _log_dir: Path) -> None:
                start_calls.append(claimed["job_id"])

            def healthy_quota_health(target_store: TaskStore) -> None:
                target_store.set_service_status(
                    "codex_quota", "healthy", "test quota ready",
                    {"remaining_percent": 100},
                )

            with patch.dict(os.environ, environment, clear=False):
                with patch("app.runner_daemon._start_worker", side_effect=start_worker):
                    with patch(
                        "app.runner_daemon._refresh_codex_health",
                        side_effect=healthy_quota_health,
                    ):
                        # A fresh Linux runner can have less uptime than the
                        # configured health-check interval. It must still run
                        # the initial quota/auth check before trying to claim.
                        with patch("app.runner_daemon.time.monotonic", return_value=10.0):
                            run_daemon(once=True)
                            run_daemon(once=True)

            saved = store.get_job(job["job_id"])
            self.assertEqual(saved["status"], JobStatus.RUNNING.value)
            self.assertEqual(len(store.list_job_attempts(job["job_id"])), 1)
            self.assertEqual(start_calls, [job["job_id"]])


if __name__ == "__main__":
    unittest.main()
