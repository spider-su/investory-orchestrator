from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.task_scheduler import _run_final_review
from app.tasks import TaskStatus, TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_persists_task_and_transition_across_store_instances(self) -> None:
        task = self.store.create(
            issue_number=42,
            title="Fix the report",
            body="Acceptance criteria",
            source="github_issue",
        )
        self.store.transition(
            task.task_id,
            TaskStatus.PLANNING,
            workspace="/tmp/task-42",
        )

        reopened = TaskStore(self.store.path)
        saved = reopened.get("42")

        self.assertIsNotNone(saved)
        self.assertEqual(saved.status, TaskStatus.PLANNING)
        self.assertEqual(saved.workspace, "/tmp/task-42")

    def test_issue_numbers_are_unique_per_repository(self) -> None:
        first = self.store.create(
            issue_number=42, title="First", repository="one/repo",
        )
        second = self.store.create(
            issue_number=42, title="Second", repository="two/repo",
        )
        duplicate = self.store.create(
            issue_number=42, title="Duplicate", repository="one/repo",
        )

        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(duplicate.task_id, first.task_id)
        self.assertEqual(self.store.get("one/repo#42").title, "First")
        self.assertEqual(self.store.get("two/repo#42").title, "Second")

    def test_migrates_legacy_global_issue_number_constraint(self) -> None:
        import sqlite3
        import time

        path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, source TEXT NOT NULL,
            issue_number INTEGER UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '', pr_number INTEGER,
            pr_url TEXT NOT NULL DEFAULT '', ci_status TEXT NOT NULL DEFAULT 'not_started',
            implementation_attempts INTEGER NOT NULL DEFAULT 0,
            validation_attempts INTEGER NOT NULL DEFAULT 0,
            ci_attempts INTEGER NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, updated_at REAL NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO tasks(task_id, source, issue_number, title, body, status, created_at, updated_at) "
            "VALUES ('42', 'github_issue', 42, 'Legacy', '', 'QUEUED', ?, ?)",
            (time.time(), time.time()),
        )
        connection.commit()
        connection.close()

        migrated = TaskStore(path)
        self.assertEqual(migrated.get("spider-su/investory#42").title, "Legacy")
        other = migrated.create(
            issue_number=42, title="Other repo", repository="other/repo",
        )
        self.assertEqual(other.repository, "other/repo")

    def test_rejects_invalid_transition(self) -> None:
        task = self.store.create(title="Run task")

        with self.assertRaisesRegex(ValueError, "QUEUED -> READY"):
            self.store.transition(task.task_id, TaskStatus.READY)

    def test_merged_completion_requires_post_merge_ci(self) -> None:
        task = self.store.create(title="Run task")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.BLOCKED)
        evidence = {
            "completion": {
                "source": "human_merge",
                "pr_number": 12,
                "merge_commit_sha": "a" * 40,
                "merged_at": "2026-10-06T10:00:00Z",
                "merged_by": "reviewer",
                "base_branch": "develop",
                "post_merge_ci_status": "success",
                "issue_closed": False,
            }
        }
        task = self.store.transition(
            task.task_id,
            TaskStatus.COMPLETED,
            pr_number=12,
            pr_url="https://example.test/pull/12",
            ci_status="green",
            metadata=evidence,
        )

        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.metadata["completion"]["merged_by"], "reviewer")

    def test_completed_rejects_orchestrator_merge_or_missing_ci(self) -> None:
        task = self.store.create(title="Run task")
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.BLOCKED)

        with self.assertRaisesRegex(ValueError, "valid merge source"):
            self.store.transition(
                task.task_id,
                TaskStatus.COMPLETED,
                pr_number=12,
                pr_url="https://example.test/pull/12",
                ci_status="pending",
                metadata={"completion": {"source": "automation"}},
            )

    def test_completed_accepts_reviewed_and_validated_no_change_outcome(self) -> None:
        task = self.store.create(
            title="No safe change", issue_number=43, source="github_issue"
        )
        task = self.store.transition(task.task_id, TaskStatus.PLANNING)
        task = self.store.transition(task.task_id, TaskStatus.IMPLEMENTING)
        task = self.store.transition(task.task_id, TaskStatus.PUBLISHING)

        completed = self.store.transition(
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
                }
            },
        )

        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertIsNone(completed.pr_number)
        self.assertEqual(completed.metadata["completion"]["outcome"], "no_changes")

    def test_service_status_persists_and_worker_heartbeat_merges_metadata(self) -> None:
        task = self.store.create(title="heartbeat", metadata={"preserved": True})

        self.store.heartbeat_worker(task.task_id, "scheduler-1", 1234)
        saved = self.store.get(task.task_id)
        status = self.store.set_service_status(
            "runner", "unavailable", "Mac SSH is unreachable.",
            {"mode": "mac_ssh", "checks": {"ssh": False}},
        )

        self.assertTrue(saved.metadata["preserved"])
        self.assertEqual(saved.metadata["lease_owner"], "scheduler-1")
        self.assertEqual(saved.metadata["worker_pid"], 1234)
        self.assertGreater(saved.metadata["worker_heartbeat_at"], 0)
        self.assertEqual(self.store.get_service_status("runner"), status)
        self.assertEqual(self.store.list_service_status(), [status])

    def test_checks_expected_status_atomically(self) -> None:
        task = self.store.create(title="Run task")

        with self.assertRaisesRegex(RuntimeError, "expected IMPLEMENTING"):
            self.store.transition(
                task.task_id,
                TaskStatus.PLANNING,
                expected=TaskStatus.IMPLEMENTING,
            )

    def test_lists_by_status(self) -> None:
        queued = self.store.create(title="queued")
        running = self.store.create(title="running")
        self.store.transition(running.task_id, TaskStatus.PLANNING)

        self.assertEqual(
            [task.task_id for task in self.store.list({TaskStatus.QUEUED})],
            [queued.task_id],
        )

    def test_ready_requires_independent_review_and_other_quality_gates(self) -> None:
        task = self._ready_for_final_review()
        review = SimpleNamespace(
            status="approved",
            model_dump=lambda mode=None: {"status": "approved"},
        )
        with (
            patch("app.agents.reviewer.review_implementation", return_value=review),
            patch("app.agents.reviewer.review_identity", return_value={
                "backend": "reviewer",
                "provider": "openai",
                "model": "review-model",
            }),
            patch("app.task_scheduler.subprocess.run", return_value=SimpleNamespace(stdout="")),
        ):
            _run_final_review(self.store, task)

        self.assertEqual(self.store.get(task.task_id).status, TaskStatus.READY)

    def test_ready_transition_rejects_missing_or_failed_ci(self) -> None:
        task = self._ready_for_final_review()
        metadata = {
            **task.metadata,
            "final_review_status": "approved",
            "final_review_independence": "independent",
            "ready_gates": {"clean_worktree": True},
        }
        for ci_status in ("not_started", "failed"):
            with self.subTest(ci_status=ci_status):
                with self.assertRaisesRegex(ValueError, "green CI"):
                    self.store.transition(
                        task.task_id,
                        TaskStatus.READY,
                        ci_status=ci_status,
                        metadata=metadata,
                    )

    def test_same_model_review_cannot_mark_task_ready(self) -> None:
        task = self._ready_for_final_review()
        review = SimpleNamespace(
            status="approved",
            model_dump=lambda mode=None: {"status": "approved"},
        )
        with (
            patch("app.agents.reviewer.review_implementation", return_value=review),
            patch("app.agents.reviewer.review_identity", return_value={
                "backend": "reviewer",
                "provider": "openai",
                "model": "coder-model",
            }),
            patch("app.task_scheduler.subprocess.run", return_value=SimpleNamespace(stdout="")),
        ):
            _run_final_review(self.store, task)

        saved = self.store.get(task.task_id)
        self.assertEqual(saved.status, TaskStatus.BLOCKED)
        self.assertIn("not independent", saved.blocked_reason)

    def _ready_for_final_review(self):
        task = self.store.create(
            issue_number=None,
            title="Persist report",
            body="Acceptance criteria",
            source="github_issue",
        )
        for status in (
            TaskStatus.PLANNING,
            TaskStatus.IMPLEMENTING,
            TaskStatus.VALIDATING,
            TaskStatus.REVIEWING,
            TaskStatus.PUBLISHING,
            TaskStatus.WAITING_CI,
            TaskStatus.FINAL_REVIEW,
        ):
            task = self.store.transition(task.task_id, status)
        return self.store.transition(
            task.task_id,
            TaskStatus.FINAL_REVIEW,
            workspace="/tmp/task-worktree",
            pr_number=99,
            pr_url="https://example.test/pr/99",
            ci_status="green",
            metadata={
                "issue_number": 88,
                "issue_title": "Persist report",
                "issue_body": "Acceptance criteria",
                "plan": {},
                "final_validation_status": "validation_success",
                "coder_provider": "openai",
                "coder_model": "coder-model",
                "ci_details": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
