from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.task_scheduler import (
    _print_task,
    _dispatch_pause_reason,
    _pause_on_codex_outage,
    _poll_ci,
    _notify_terminal_tasks,
    _poll_ready_issues,
    _pull_job_spec,
    _queue_pull_runner_job,
    _reconcile_pull_runner_jobs,
    _run_final_review,
    _remote_worker_command,
    _remote_worker_is_running,
    _refresh_runner_health,
    _sync_task_result,
    _track_task_state,
    reconcile_merged_task,
    run_queue,
)
from app.tasks import JobKind, JobStatus, TaskStatus, TaskStore


VALID_READY_ISSUE_BODY = """## Goal
Implement the requested behavior.

## Context
The current behavior does not satisfy the requested outcome.

## Product decisions
Keep the existing user-visible behavior unless the acceptance criteria say otherwise.

## Scope
### In scope
- Implement the requested behavior.
### Out of scope
- Unrelated product changes.

## Acceptance criteria
- The requested behavior is observable.

## Validation
- Run focused automated tests.

## Change constraints
- Database migration allowed: no
- Breaking API change allowed: no
- Dependency changes allowed: no
- Configuration changes allowed: no
"""


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository_env = patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "spider-su/investory"}, clear=False
        )
        self.repository_env.start()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")
        from app.task_scheduler import _last_repository_poll

        _last_repository_poll.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        self.repository_env.stop()

    def test_ci_poll_ignores_running_tasks_outside_post_ci_review_phase(self) -> None:
        task = self.store.create(title="Still implementing")
        self.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            metadata={**task.metadata, "phase": "implementing"},
        )
        with patch("app.task_scheduler._poll_merged_tasks"):
            with patch("app.github_client.GitHubAppClient") as client:
                _poll_ci(self.store)

        client.assert_not_called()

    def test_resuming_status_clears_stale_blocked_details(self) -> None:
        task = self.store.create(title="resume", issue_number=104)
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            expected=TaskStatus.QUEUED,
            blocked_reason="old failure",
            metadata={"blocked_stage": "complete_step"},
        )

        _track_task_state(self.store, task.task_id, TaskStatus.IMPLEMENTING)
        current = self.store.get(task.task_id)

        self.assertEqual(current.status, TaskStatus.IMPLEMENTING)
        self.assertEqual(current.blocked_reason, "")
        self.assertNotIn("blocked_stage", current.metadata)

    def test_task_output_hides_block_reason_after_recovery(self) -> None:
        task = self.store.create(title="resume", issue_number=105)
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            expected=TaskStatus.QUEUED,
            blocked_reason="old failure",
        )
        task = self.store.transition(
            task.task_id,
            TaskStatus.IMPLEMENTING,
            expected=TaskStatus.BLOCKED,
        )

        from io import StringIO
        from contextlib import redirect_stdout

        output = StringIO()
        with redirect_stdout(output):
            _print_task(task)
        self.assertNotIn("old failure", output.getvalue())

    def test_completed_task_status_prints_merge_summary(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        task = SimpleNamespace(
            task_id="spider-su/investory#104",
            status=TaskStatus.COMPLETED,
            title="Completed task",
            pr_number=105,
            pr_url="https://github.com/spider-su/investory/pull/105",
            ci_status="green",
            blocked_reason="",
            metadata={
                "completion": {
                    "merge_commit_sha": "a" * 40,
                    "merged_by": "spider-su",
                    "issue_closed": True,
                },
                "plan": {},
                "final_review": {},
            },
        )
        output = StringIO()
        with redirect_stdout(output):
            _print_task(task)

        self.assertIn("DONE", output.getvalue())
        self.assertIn("Merged by: spider-su", output.getvalue())
        self.assertIn("Issue closed: True", output.getvalue())
        self.assertIn("Task completed after human merge", output.getvalue())
        self.assertNotIn("Human action:", output.getvalue())

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
        self.assertEqual(command[-1], "run 42 42 - 0 0 unknown")

    def test_pull_dispatch_persists_job_and_task_reservation(self) -> None:
        task = self.store.create(
            issue_number=208, title="Pull task", body="Acceptance criteria",
            repository="spider-su/investory",
            metadata={"base_branch": "develop"},
        )
        with patch.dict(os.environ, {"RUNNER_TRANSPORT": "postgres_pull"}, clear=False):
            job = _queue_pull_runner_job(
                self.store, task, expected_status=TaskStatus.QUEUED,
                claimed_status=TaskStatus.PLANNING, kind=JobKind.IMPLEMENT,
            )

        current = self.store.get(task.task_id)
        self.assertEqual(current.status, TaskStatus.PLANNING)
        self.assertEqual(current.metadata["worker_mode"], "postgres_pull")
        self.assertEqual(current.metadata["runner_job_id"], job["job_id"])
        self.assertEqual(job["status"], JobStatus.PENDING.value)
        self.assertEqual(job["task_branch"], "agent/issue-208")
        self.assertEqual(job["required_capabilities"], ["build", "codex", "git", "review"])
        self.assertNotIn("token", str(job["job_spec"]).casefold())

    def test_pull_queue_enqueues_without_starting_synchronous_worker(self) -> None:
        repository = "spider-su/investory"
        self.store.save_repository({
            "repository": repository, "enabled": True, "base_branch": "develop",
        })
        self.store.register_runner(
            "pull-runner", capabilities={"codex", "git", "build", "review"},
        )
        self.store.set_service_status("runner", "ready", "test runner online", {})
        self.store.set_service_status(
            "codex_quota", "healthy", "test quota available",
            {"remaining_percent": 100},
        )
        task = self.store.create(
            issue_number=211, title="Queued pull task", body="Prompt",
            repository=repository,
        )
        environment = {
            "RUNNER_TRANSPORT": "postgres_pull",
            "MAX_ACTIVE_TASKS": "1",
            "MAX_CODEX_PROCESSES": "1",
            "MAX_BUILDS": "1",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch("app.task_scheduler._poll_ready_issues"):
                with patch("app.task_scheduler._refresh_runner_health"):
                    with patch("app.task_scheduler._poll_ci"):
                        with patch("app.task_scheduler._notify_terminal_tasks"):
                            with patch(
                                "app.task_scheduler.subprocess.Popen",
                                side_effect=AssertionError("pull mode must not spawn SSH/local workers"),
                            ):
                                run_queue(self.store, once=True)

        current = self.store.get(task.task_id)
        self.assertEqual(current.status, TaskStatus.PLANNING)
        self.assertEqual(current.metadata["worker_mode"], "postgres_pull")
        self.assertEqual(
            self.store.get_job(current.metadata["runner_job_id"])["status"],
            JobStatus.PENDING.value,
        )

    def test_pull_job_reconciliation_blocks_when_reserved_job_is_missing(self) -> None:
        task = self.store.create(
            title="Reserved task", body="Prompt", repository="spider-su/investory",
        )
        job = _queue_pull_runner_job(
            self.store, task, expected_status=TaskStatus.QUEUED,
            claimed_status=TaskStatus.PLANNING, kind=JobKind.IMPLEMENT,
        )
        with self.store._connection() as connection:
            connection.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))

        _reconcile_pull_runner_jobs(self.store)

        self.assertIsNone(self.store.get_job(job["job_id"]))
        current = self.store.get(task.task_id)
        self.assertEqual(current.status, TaskStatus.BLOCKED)
        self.assertIn("previous process state cannot be proven", current.blocked_reason)
        self.assertEqual(
            current.metadata["uncertain_runner_job"]["job_id"], job["job_id"],
        )
        self.assertIsInstance(
            current.metadata["uncertain_runner_job"]["job_spec"], dict,
        )

    def test_pull_job_is_reused_after_scheduler_restart_before_task_reservation(self) -> None:
        task = self.store.create(
            title="Pending job after restart", body="Prompt",
            repository="spider-su/investory",
        )
        expected = _pull_job_spec(
            self.store, task, kind=JobKind.IMPLEMENT, sequence=0,
        )
        queued = self.store.enqueue_job(expected, job_id=expected["job_id"])

        resumed = _queue_pull_runner_job(
            self.store, task, expected_status=TaskStatus.QUEUED,
            claimed_status=TaskStatus.PLANNING, kind=JobKind.IMPLEMENT,
        )

        self.assertEqual(resumed["job_id"], queued["job_id"])
        self.assertEqual(self.store.get(task.task_id).status, TaskStatus.PLANNING)
        self.assertEqual(len(self.store.list_job_attempts(queued["job_id"])), 0)

    def test_pull_job_completion_is_reconciled_without_retrying_running_work(self) -> None:
        task = self.store.create(
            issue_number=209, title="Completed remote task", body="Prompt",
            repository="spider-su/investory",
        )
        job = _queue_pull_runner_job(
            self.store, task, expected_status=TaskStatus.QUEUED,
            claimed_status=TaskStatus.PLANNING, kind=JobKind.IMPLEMENT,
        )
        self.store.register_runner(
            "runner-1", capabilities={"codex", "git", "build", "review"},
        )
        claimed = self.store.claim_job("runner-1")
        self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)
        self.store.transition(task.task_id, TaskStatus.VALIDATING)
        self.store.transition(task.task_id, TaskStatus.REVIEWING)
        self.store.transition(task.task_id, TaskStatus.PUBLISHING)
        self.store.transition(task.task_id, TaskStatus.WAITING_CI, ci_status="queued")
        self.store.complete_job(
            job["job_id"], "runner-1", claimed["current_attempt_id"],
            result={"task_status": "WAITING_CI"},
        )

        _reconcile_pull_runner_jobs(self.store)

        current = self.store.get(task.task_id)
        self.assertEqual(current.status, TaskStatus.WAITING_CI)
        self.assertNotIn("runner_job_id", current.metadata)
        self.assertEqual(current.metadata["last_runner_job"]["status"], "succeeded")
        self.assertEqual(current.metadata["runner_dispatch_sequence"], 1)

    def test_pull_final_review_is_queued_and_reconciled_asynchronously(self) -> None:
        task = self.store.create(
            issue_number=210, title="Final review task", body="Prompt",
            repository="spider-su/investory",
            metadata={
                "base_branch": "develop",
                "issue_number": 210,
                "issue_title": "Final review task",
                "issue_body": "Prompt",
                "plan": {"summary": "Fixture plan", "steps": []},
                "final_validation_status": "validation_success",
                "final_validation_output": "validation passed",
                "coder_model": "codex-coder",
                "coder_provider": "codex-cli",
            },
        )
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)
        task = self.store.transition(task.task_id, TaskStatus.VALIDATING)
        task = self.store.transition(task.task_id, TaskStatus.REVIEWING)
        task = self.store.transition(task.task_id, TaskStatus.PUBLISHING)
        task = self.store.transition(
            task.task_id, TaskStatus.WAITING_CI, ci_status="green",
            workspace="/runner/workspaces/issue-210", branch="agent/issue-210",
            pr_number=210, pr_url="https://github.com/spider-su/investory/pull/210",
        )
        task = self.store.transition(task.task_id, TaskStatus.FINAL_REVIEW)
        github = SimpleNamespace(get_pull_request_details=lambda _number: {
            "is_merged": False,
            "state": "open",
            "base_ref": "develop",
            "head_ref": "agent/issue-210",
            "head_sha": "b" * 40,
        })
        environment = {
            "RUNNER_TRANSPORT": "postgres_pull",
            "GITHUB_APP_ID": "test-app",
            "BASE_BRANCH": "develop",
            "REVIEWER_MODEL": "codex-reviewer",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch("app.github_client.GitHubAppClient", return_value=github):
                _run_final_review(self.store, task)

        waiting = self.store.get(task.task_id)
        job_id = waiting.metadata["final_review_job_id"]
        review_job = self.store.get_job(job_id)
        self.assertEqual(review_job["kind"], JobKind.REVIEW.value)
        self.assertEqual(review_job["status"], JobStatus.PENDING.value)
        self.assertEqual(waiting.status, TaskStatus.FINAL_REVIEW)

        self.store.register_runner(
            "review-runner", capabilities={"codex", "git", "review"},
        )
        claimed = self.store.claim_job("review-runner")
        review_result = {
            "task_id": task.task_id,
            "head_sha": "b" * 40,
            "branch": "agent/issue-210",
            "clean_worktree": True,
            "review": {"status": "approved", "summary": "Looks correct."},
            "reviewer_identity": {
                "backend": "codex-cli", "provider": "codex-cli",
                "model": "codex-reviewer",
            },
        }
        self.store.complete_job(
            job_id, "review-runner", claimed["current_attempt_id"],
            result={"worker_result": review_result},
        )
        with patch.dict(os.environ, environment, clear=False):
            with patch("app.github_client.GitHubAppClient", return_value=github):
                _run_final_review(self.store, self.store.get(task.task_id))

        completed_review = self.store.get(task.task_id)
        self.assertEqual(completed_review.status, TaskStatus.READY)
        self.assertEqual(completed_review.metadata["final_review_head_sha"], "b" * 40)
        self.assertNotIn("final_review_job_id", completed_review.metadata)

    def test_missing_reserved_pull_review_is_blocked_without_requeue(self) -> None:
        task = self.store.create(
            issue_number=211, title="Review recovery safety", body="Prompt",
            repository="spider-su/investory",
            metadata={
                "base_branch": "develop",
                "issue_number": 211,
                "issue_title": "Review recovery safety",
                "issue_body": "Prompt",
                "plan": {"summary": "Fixture plan", "steps": []},
                "final_validation_status": "validation_success",
                "final_review_job_id": "a" * 32,
            },
        )
        task = self.store.transition(task.task_id, TaskStatus.RUNNING)
        task = self.store.transition(
            task.task_id,
            TaskStatus.WAITING_CI,
            ci_status="green",
            workspace="/runner/workspaces/issue-211",
            branch="agent/issue-211",
            pr_number=211,
            pr_url="https://github.com/spider-su/investory/pull/211",
        )
        github = SimpleNamespace(get_pull_request_details=lambda _number: {
            "is_merged": False,
            "state": "open",
            "base_ref": "develop",
            "head_ref": "agent/issue-211",
            "head_sha": "c" * 40,
        })
        with patch.dict(os.environ, {"RUNNER_TRANSPORT": "postgres_pull"}, clear=False):
            with patch("app.github_client.GitHubAppClient", return_value=github):
                _run_final_review(self.store, task)

        current = self.store.get(task.task_id)
        self.assertEqual(current.status, TaskStatus.BLOCKED)
        self.assertIn("will not be dispatched again", current.blocked_reason)
        self.assertIsNone(self.store.get_job("a" * 32))
        self.assertEqual(
            current.metadata["uncertain_final_review_job"]["head_sha"],
            "c" * 40,
        )

    def test_remote_worker_rejects_invalid_ssh_target(self) -> None:
        task = self.store.create(title="remote", issue_number=43)
        with patch.dict(
            os.environ,
            {"MAC_SSH_TARGET": "-oProxyCommand=bad"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "SSH user and host"):
                _remote_worker_command(task)

    def test_runner_health_is_persisted_for_dashboard(self) -> None:
        report = {
            "status": "ready",
            "detail": "Mac runner ready at 123456789abc (codex 1.2.3).",
            "checks": {"ssh": True, "codex_authenticated": True},
        }
        with (
            patch.dict(os.environ, {
                "MAC_SSH_TARGET": "codex@192.168.1.7",
                "ORCHESTRATOR_BUILD_SHA": "a" * 40,
            }),
            patch(
                "app.task_scheduler.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(report)),
            ) as run,
        ):
            _refresh_runner_health(self.store, force=True)

        self.assertIn("health", run.call_args.args[0][-1])
        status = self.store.get_service_status("runner")
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["metadata"]["checks"]["ssh"], True)

    def test_codex_quota_failure_pauses_dispatch_globally(self) -> None:
        task = self.store.create(title="Codex quota")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            blocked_reason="Codex failed: 429 rate_limit_exceeded",
        )

        _pause_on_codex_outage(self.store, task)

        status = self.store.get_service_status("codex_queue")
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["metadata"]["category"], "quota")
        self.assertIn("Wait for quota recovery", _dispatch_pause_reason(self.store))

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
        self.assertEqual(client_type.return_value.upsert_issue_comment.call_count, 2)
        body = client_type.return_value.upsert_issue_comment.call_args.args[1]
        self.assertIn("@spider-su", body)
        self.assertIn("Needs a human decision", body)

    def test_ready_issue_polling_is_repo_scoped_idempotent_and_interval_limited(self) -> None:
        self.store.save_repository({
            "repository": "other/project", "enabled": True,
            "base_branch": "develop", "poll_interval_seconds": 60,
        })
        issue = SimpleNamespace(
            number=7, title="Ready fixture", body=VALID_READY_ISSUE_BODY,
        )
        clients = {}

        class FakeClient:
            def __init__(self, repository):
                self.repository = repository
                self.removed = []
                self.comments = []
                self.list_calls = 0
                clients[repository] = self

            def list_ready_issues(self, label):
                self.list_calls += 1
                self.asserted_label = label
                return [issue]

            def remove_issue_label(self, issue_number, label):
                self.removed.append((issue_number, label))

            def update_issue_body(self, issue_number, body):
                issue.body = body

            def upsert_issue_comment(self, issue_number, body, *, marker):
                self.comments.append((issue_number, body, marker))

        with (
            patch.dict(os.environ, {"READY_ISSUE_LABEL": "ready_to_develop"}),
            patch("app.github_client.GitHubAppClient", FakeClient),
        ):
            self.assertEqual(_poll_ready_issues(self.store, now=1000), 2)
            self.assertEqual(_poll_ready_issues(self.store, now=1010), 0)

        first = self.store.get("spider-su/investory#7")
        second = self.store.get("other/project#7")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(first.metadata["base_branch"], "develop")
        self.assertEqual(clients["spider-su/investory"].list_calls, 1)
        self.assertEqual(clients["other/project"].list_calls, 1)
        self.assertEqual(clients["spider-su/investory"].removed, [(7, "ready_to_develop")])
        self.assertIn("QUEUED", clients["spider-su/investory"].comments[0][1])

    def test_ready_issue_poll_retries_label_ack_without_duplicate_task(self) -> None:
        issue = SimpleNamespace(
            number=8, title="Retry ack", body=VALID_READY_ISSUE_BODY
        )
        client = SimpleNamespace(
            list_ready_issues=lambda label: [issue],
            remove_issue_label=unittest.mock.Mock(side_effect=[RuntimeError("temporary"), None]),
            upsert_issue_comment=unittest.mock.Mock(),
            update_issue_body=unittest.mock.Mock(),
        )
        with (
            patch.dict(os.environ, {"READY_ISSUE_LABEL": "ready_to_develop"}),
            patch("app.github_client.GitHubAppClient", return_value=client),
        ):
            _poll_ready_issues(self.store, now=2000)
            _poll_ready_issues(self.store, now=2060)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(client.remove_issue_label.call_count, 2)

    def test_malformed_ready_issue_is_formatted_and_queued(self) -> None:
        issue = SimpleNamespace(
            number=9,
            title="Incomplete issue",
            body="Only a vague request.",
            labels=[SimpleNamespace(name="ready_to_develop")],
        )
        client = SimpleNamespace(
            list_ready_issues=lambda label: [issue],
            remove_issue_label=unittest.mock.Mock(),
            upsert_issue_comment=unittest.mock.Mock(),
            update_issue_body=unittest.mock.Mock(),
        )
        with (
            patch.dict(os.environ, {"READY_ISSUE_LABEL": "ready_to_develop"}),
            patch("app.github_client.GitHubAppClient", return_value=client),
        ):
            queued = _poll_ready_issues(self.store, now=3000)

        self.assertEqual(queued, 1)
        task = self.store.get("spider-su/investory#9")
        self.assertIsNotNone(task)
        self.assertIn("## Acceptance criteria", task.body)
        self.assertIn("## Original issue description\nOnly a vague request.", task.body)
        client.update_issue_body.assert_called_once()
        client.remove_issue_label.assert_called_once_with(9, "ready_to_develop")

    def test_queue_obeys_build_limit(self) -> None:
        first = self.store.create(title="first")
        self.store.create(title="second")
        process = SimpleNamespace(pid=987654, wait=lambda: 0)
        with (
            patch.dict(os.environ, {"MAX_ACTIVE_TASKS": "3", "MAX_CODEX_PROCESSES": "3", "MAX_BUILDS": "1"}),
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ready_issues"),
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
            patch("app.task_scheduler._poll_ready_issues"),
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
                "coder_provider": "codex-cli",
                "coder_model": "gpt-6.1-sol",
            },
        )

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.WAITING_CI)
        self.assertEqual(saved.pr_number, 18)
        self.assertEqual(saved.ci_status, "queued")
        self.assertEqual(saved.metadata["coder_provider"], "codex-cli")
        self.assertEqual(saved.metadata["coder_model"], "gpt-6.1-sol")

    def test_blocked_workflow_persists_coder_identity_for_final_review_gate(self) -> None:
        task = self.store.create(title="Blocked with coder evidence")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)

        _sync_task_result(
            self.store,
            task.task_id,
            {
                "workflow_status": "blocked",
                "blocked_reason": "Validation requires a repair.",
                "blocked_stage": "validation",
                "coder_provider": "codex-cli",
                "coder_model": "gpt-6.1-sol",
            },
        )

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.BLOCKED)
        self.assertEqual(saved.metadata["coder_provider"], "codex-cli")
        self.assertEqual(saved.metadata["coder_model"], "gpt-6.1-sol")

    def test_no_change_workflow_completes_without_pr_or_issue_close(self) -> None:
        task = self.store.create(
            title="No safe change", issue_number=45, source="github_issue"
        )
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)

        _sync_task_result(
            self.store,
            task.task_id,
            {
                "workflow_status": "completed",
                "no_change_outcome": True,
                "issue_number": 45,
                "issue_title": "No safe change",
                "branch": "agent/issue-45",
                "final_validation_status": "validation_success",
                "final_review_status": "approved",
                "final_review": {"summary": "No candidate was proven safe."},
            },
        )

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.COMPLETED)
        self.assertIsNone(saved.pr_number)
        self.assertEqual(saved.ci_status, "not_required")
        self.assertFalse(saved.metadata["completion"]["issue_closed"])

    def test_no_change_completion_notification_explains_no_pr(self) -> None:
        task = self.store.create(
            title="No safe change", issue_number=46, source="github_issue"
        )
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)
        task = self.store.transition(task.task_id, TaskStatus.PUBLISHING)
        task = self.store.transition(
            task.task_id,
            TaskStatus.COMPLETED,
            pr_number=None,
            pr_url="",
            ci_status="not_required",
            metadata={
                "completion": {
                    "outcome": "no_changes",
                    "summary": "No candidate was proven safe to remove.",
                    "validation_status": "validation_success",
                    "review_status": "approved",
                    "issue_closed": False,
                },
                "release_promotion": {"status": "not_required"},
            },
        )
        with patch("app.github_client.GitHubAppClient") as client_type:
            _notify_terminal_tasks(self.store)
        posted = [
            call.args[1]
            for call in client_type.return_value.upsert_issue_comment.call_args_list
        ]
        self.assertTrue(any("completed with no code changes" in body for body in posted))
        self.assertTrue(any("No PR was created" in body for body in posted))

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

    def test_manual_merge_reconciliation_closes_issue_after_successful_ci(self) -> None:
        task = self.store.create(
            title="merged task",
            issue_number=104,
            repository="spider-su/investory",
            source="github_issue",
            metadata={"base_branch": "develop"},
        )
        for status in (TaskStatus.PLANNING, TaskStatus.IMPLEMENTING, TaskStatus.BLOCKED):
            task = self.store.transition(task.task_id, status)
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            pr_number=105,
            pr_url="https://github.com/spider-su/investory/pull/105",
        )
        client = SimpleNamespace(
            get_pull_request_details=lambda number: {
                "number": number,
                "is_merged": True,
                "base_ref": "develop",
                "head_ref": task.branch,
                "head_sha": "b" * 40,
                "merge_commit_sha": "c" * 40,
                "merged_by": "spider-su",
                "merged_at": "2026-10-06T10:00:00Z",
                "body": "Closes spider-su/investory#104",
            },
            get_commit_ci=lambda sha: ("success", [{"sha": sha}]),
            close_issue=lambda number: True,
        )
        with patch("app.github_client.GitHubAppClient", return_value=client):
            completed = reconcile_merged_task(self.store, task.task_id)

        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.metadata["completion"]["recorded_via"], "manual_reconciliation")
        self.assertTrue(completed.metadata["completion"]["issue_closed"])

    def test_ci_failure_dispatches_a_repair_worker(self) -> None:
        task = self.store.create(title="repair CI")
        task = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            ci_status="failed",
            ci_attempts=1,
            implementation_attempts=1,
        )
        process = SimpleNamespace(pid=987656, wait=lambda: 0)
        with (
            patch("app.task_scheduler.subprocess.Popen", return_value=process) as popen,
            patch("app.task_scheduler._poll_ready_issues"),
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
            patch("app.task_scheduler._poll_ready_issues"),
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
            ci_attempts=1,
            implementation_attempts=2,
        )
        review_task = self.store.create(title="exhausted review")
        self.store.transition(
            review_task.task_id,
            TaskStatus.BLOCKED,
            metadata={
                "final_review_status": "changes_required",
                "final_review_repairs": 1,
            },
            implementation_attempts=2,
        )
        with (
            patch.dict(os.environ, {"CI_RETRY_ATTEMPTS": "3"}),
            patch("app.task_scheduler.subprocess.Popen") as popen,
            patch("app.task_scheduler._poll_ready_issues"),
            patch("app.task_scheduler._poll_ci"),
        ):
            run_queue(self.store, once=True)

        popen.assert_not_called()
        self.assertEqual(self.store.get(ci_task.task_id).status, TaskStatus.BLOCKED)
        self.assertEqual(self.store.get(review_task.task_id).status, TaskStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
