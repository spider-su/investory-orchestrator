from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    PUBLISHING = "PUBLISHING"
    WAITING_CI = "WAITING_CI"
    FINAL_REVIEW = "FINAL_REVIEW"
    READY = "READY"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.PLANNING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.IMPLEMENTING: frozenset({TaskStatus.VALIDATING, TaskStatus.PUBLISHING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.VALIDATING: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.PUBLISHING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.PUBLISHING: frozenset({TaskStatus.WAITING_CI, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.WAITING_CI: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.FINAL_REVIEW, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.FINAL_REVIEW: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.READY: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING, TaskStatus.VALIDATING, TaskStatus.REVIEWING, TaskStatus.PUBLISHING, TaskStatus.WAITING_CI, TaskStatus.FINAL_REVIEW, TaskStatus.READY, TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED}),
}


@dataclass(frozen=True)
class Task:
    task_id: str
    source: str
    issue_number: int | None
    title: str
    body: str
    status: TaskStatus
    workspace: str
    branch: str
    pr_number: int | None
    pr_url: str
    ci_status: str
    implementation_attempts: int
    validation_attempts: int
    ci_attempts: int
    blocked_reason: str
    metadata: dict[str, Any]
    created_at: float
    updated_at: float


class TaskStore:
    """Durable task aggregates with application-owned transition checks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    issue_number INTEGER UNIQUE,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workspace TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    pr_number INTEGER,
                    pr_url TEXT NOT NULL DEFAULT '',
                    ci_status TEXT NOT NULL DEFAULT 'not_started',
                    implementation_attempts INTEGER NOT NULL DEFAULT 0,
                    validation_attempts INTEGER NOT NULL DEFAULT 0,
                    ci_attempts INTEGER NOT NULL DEFAULT 0,
                    blocked_reason TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tasks_status_created "
                "ON tasks(status, created_at)"
            )

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        values = dict(row)
        values["status"] = TaskStatus(values["status"])
        values["metadata"] = json.loads(values["metadata"])
        return Task(**values)

    def create(
        self,
        *,
        title: str,
        body: str = "",
        issue_number: int | None = None,
        source: str = "prompt",
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        if issue_number is not None:
            existing = self.get(str(issue_number))
            if existing is not None:
                return existing
        now = time.time()
        task_id = str(issue_number) if issue_number is not None else uuid.uuid4().hex[:12]
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO tasks
                (task_id, source, issue_number, title, body, status, metadata,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, source, issue_number, title, body,
                 TaskStatus.QUEUED.value, json.dumps(metadata or {}), now, now),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)

    def get(self, task_id: str) -> Task | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=? OR issue_number=?",
                (task_id, task_id if task_id.isdigit() else None),
            ).fetchone()
        return self._task(row) if row else None

    def list(self, statuses: set[TaskStatus] | None = None) -> list[Task]:
        with self._connection() as connection:
            if statuses:
                values = [status.value for status in statuses]
                marks = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"SELECT * FROM tasks WHERE status IN ({marks}) "
                    "ORDER BY created_at", values,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at"
                ).fetchall()
        return [self._task(row) for row in rows]

    def transition(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        expected: TaskStatus | None = None,
        **updates: Any,
    ) -> Task:
        allowed_fields = {
            "workspace", "branch", "pr_number", "pr_url", "ci_status",
            "implementation_attempts", "validation_attempts", "ci_attempts",
            "blocked_reason", "metadata", "title", "body",
        }
        unknown = set(updates) - allowed_fields
        if unknown:
            raise ValueError(f"Unsupported task fields: {sorted(unknown)}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            current = TaskStatus(row["status"])
            if expected is not None and current != expected:
                raise RuntimeError(
                    f"Task {task_id} is {current.value}; expected {expected.value}"
                )
            if status != current and status not in TRANSITIONS[current]:
                raise ValueError(
                    f"Invalid task transition: {current.value} -> {status.value}"
                )
            if status == TaskStatus.READY:
                metadata_value = updates.get("metadata")
                metadata = (
                    metadata_value
                    if isinstance(metadata_value, dict)
                    else json.loads(metadata_value or row["metadata"])
                )
                ci_status = updates.get("ci_status", row["ci_status"])
                missing = []
                if ci_status != "green":
                    missing.append("green CI")
                if not updates.get("pr_number", row["pr_number"]) or not updates.get("pr_url", row["pr_url"]):
                    missing.append("draft PR")
                if metadata.get("final_validation_status") != "validation_success":
                    missing.append("successful local validation")
                if metadata.get("final_review_status") != "approved":
                    missing.append("approved independent review")
                if metadata.get("final_review_independence") != "independent":
                    missing.append("reviewer independence")
                if not metadata.get("ready_gates", {}).get("clean_worktree"):
                    missing.append("clean worktree evidence")
                if missing:
                    raise ValueError("Cannot mark task READY without " + ", ".join(missing))
            serialized = {
                key: json.dumps(value) if key == "metadata" else value
                for key, value in updates.items()
            }
            serialized["status"] = status.value
            serialized["updated_at"] = time.time()
            assignments = ", ".join(f"{key}=?" for key in serialized)
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id=?",
                (*serialized.values(), task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)
