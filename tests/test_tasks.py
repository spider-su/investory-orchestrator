from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
