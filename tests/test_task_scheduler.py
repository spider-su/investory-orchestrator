from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.task_scheduler import (
    _poll_ci,
    _notify_terminal_tasks,
    _remote_worker_command,
    _remote_worker_is_running,
    _sync_task_result,
    run_queue,
)
from app.tasks import TaskStatus, TaskStore


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_remote_worker_command_uses_quoted_configured_mac_paths(self) -> None:
        task = self.store.create(title="remote", issue_number=42)
        with patch.dict(
            os.environ,
            {
                "MAC_SSH_TARGET": "codex@192.168.1.7",
                "MAC_SSH_KEY_PATH": "/run/secrets/mac-key",
                "MAC_SSH_KNOWN_HOSTS": "/run/secrets/known-hosts",
            },
            clear=False,
        ):
            command = _remote_worker_command(task)
        self.assertEqual(command[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("codex@192.168.1.7", command)
        self.assertEqual(command[-1], "run 42 42 - 0 0")

    def test_remote_worker_rejects_invalid_ssh_target(self) -> None:
        task = self.store.create(title="remote", issue_number=43)
        with patch.dict(
            os.environ,
            {"MAC_SSH_TARGET": "-oProxyCommand=bad"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "SSH user and host"):
                _remote_worker_command(task)

    def test_remote_probe_fails_closed_on_ssh_error(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "MAC_SSH_TARGET": "codex@192.168.1.7",
                },
                clear=False,
            ),
            patch("app.task_scheduler.subprocess.run", return_value=SimpleNamespace(returncode=255)),
        ):
            self.assertTrue(_remote_worker_is_running("spider-su/investory#42"))

    def test_blocked_issue_gets_one_stable_github_mention(self) -> None:
        task = self.store.create(title="blocked", issue_number=44)
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            blocked_reason="Needs a human decision",
        )
        with patch("app.github_client.GitHubAppClient") as client_type:
            _notify_terminal_tasks(self.store)
            _notify_terminal_tasks(self.store)
        client_type.return_value.upsert_issue_comment.assert_called_once()
        body = client_type.return_value.upsert_issue_comment.call_args.args[1]
        self.assertIn("@spider-su", body)
        self.assertIn("Needs a human decision", body)

    def test_queue_obeys_build_limit(self) -> None:
        first = self.store.create(title="first")
        self.store.create(title="second")
        process = SimpleNamespace(pid=987654, wait=lambda: 0)
        with (
            patch.dict(os.environ, {"MAX_ACTIVE_TASKS": "3", "MAX_CODEX_PROCESSES": "3", "MAX_BUILDS": "1"}),
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        popen.assert_called_once()
        self.assertIn(first.task_id, popen.call_args.args[0])
        self.assertEqual(self.store.get(first.task_id).status, TaskStatus.PLANNING)
        self.assertEqual(self.store.list({TaskStatus.QUEUED})[0].title, "second")

    def test_queue_recovers_stale_worker_checkpoint(self) -> None:
        task = self.store.create(title="recovery")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(
            task.task_id,
            TaskStatus.IMPLEMENTING,
            metadata={"worker_pid": 2_000_000_000},
        )
        process = SimpleNamespace(pid=987655, wait=lambda: 0)
        with (
            patch("app.task_scheduler._pid_alive", return_value=False),
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        command = popen.call_args.args[0]
        self.assertIn("--resume", command)
        self.assertEqual(self.store.get(task.task_id).status, TaskStatus.IMPLEMENTING)

    def test_completed_checkpoint_reconciles_task_record_after_restart(self) -> None:
        task = self.store.create(title="finished")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)
        _sync_task_result(
            self.store,
            task.task_id,
            {
                "workflow_status": "completed",
                "pull_request_number": 18,
                "pull_request_url": "https://example.test/pr/18",
                "workspace": "/tmp/worktree",
                "branch": "codex/task",
                "final_validation_status": "validation_success",
                "final_review_status": "approved",
            },
        )

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.WAITING_CI)
        self.assertEqual(saved.pr_number, 18)
        self.assertEqual(saved.ci_status, "queued")

    def test_failed_ci_consumes_attempt_and_blocks_for_repair(self) -> None:
        task = self.store.create(title="ci")
        for status in (
            TaskStatus.PLANNING,
            TaskStatus.IMPLEMENTING,
            TaskStatus.VALIDATING,
            TaskStatus.REVIEWING,
            TaskStatus.PUBLISHING,
            TaskStatus.WAITING_CI,
        ):
            task = self.store.transition(task.task_id, status)
        task = self.store.transition(
            task.task_id,
            TaskStatus.WAITING_CI,
            pr_number=22,
            pr_url="https://example.test/pr/22",
        )
        client = SimpleNamespace(
            get_pull_request_ci=lambda _number: ("failure", [{"name": "tests", "conclusion": "failure"}])
        )
        with patch("app.github_client.GitHubAppClient", return_value=client):
            _poll_ci(self.store)

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.BLOCKED)
        self.assertEqual(saved.ci_status, "failed")
        self.assertEqual(saved.ci_attempts, 1)

    def test_ci_failure_dispatches_a_repair_worker(self) -> None:
        task = self.store.create(title="repair CI")
        task = self.store.transition(task.task_id, TaskStatus.BLOCKED, ci_status="failed", ci_attempts=1)
        process = SimpleNamespace(pid=987656, wait=lambda: 0)
        with (
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        command = popen.call_args.args[0]
        self.assertIn("--resume", command)
        self.assertIn("--ci-repair", command)
        self.assertEqual(self.store.get(task.task_id).status, TaskStatus.IMPLEMENTING)

    def test_final_review_finding_dispatches_a_repair_worker(self) -> None:
        task = self.store.create(title="repair review")
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            metadata={
                "final_review_status": "changes_required",
                "final_review_repairs": 1,
                "final_review_feedback": "Fix the edge case.",
            },
        )
        process = SimpleNamespace(pid=987657, wait=lambda: 0)
        with (
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        command = popen.call_args.args[0]
        self.assertIn("--resume", command)
        self.assertIn("--ci-repair", command)
        self.assertEqual(self.store.get(task.task_id).status, TaskStatus.IMPLEMENTING)

    def test_exhausted_repair_attempts_are_not_dispatched(self) -> None:
        ci_task = self.store.create(title="exhausted CI")
        self.store.transition(
            ci_task.task_id,
            TaskStatus.BLOCKED,
            ci_status="failed",
            ci_attempts=4,
        )
        review_task = self.store.create(title="exhausted review")
        self.store.transition(
            review_task.task_id,
            TaskStatus.BLOCKED,
            metadata={
                "final_review_status": "changes_required",
                "final_review_repairs": 4,
            },
        )
        with (
            patch.dict(os.environ, {"CI_RETRY_ATTEMPTS": "3"}),
            patch("app.task_scheduler.subprocess.Popen") as popen,
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        popen.assert_not_called()
        self.assertEqual(self.store.get(ci_task.task_id).status, TaskStatus.BLOCKED)
        self.assertEqual(self.store.get(review_task.task_id).status, TaskStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
