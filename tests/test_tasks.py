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

    def test_rejects_invalid_transition(self) -> None:
        task = self.store.create(title="Run task")

        with self.assertRaisesRegex(ValueError, "QUEUED -> READY"):
            self.store.transition(task.task_id, TaskStatus.READY)

    def test_completed_requires_human_merge_and_successful_post_merge_ci(self) -> None:
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
