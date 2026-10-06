from __future__ import annotations

import json
import os
import re
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
    COMPLETED = "COMPLETED"
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
    TaskStatus.READY: frozenset({TaskStatus.COMPLETED, TaskStatus.BLOCKED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING, TaskStatus.VALIDATING, TaskStatus.REVIEWING, TaskStatus.PUBLISHING, TaskStatus.WAITING_CI, TaskStatus.FINAL_REVIEW, TaskStatus.READY, TaskStatus.COMPLETED, TaskStatus.FAILED}),
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
    repository: str = "spider-su/investory"
    priority: int = 0


class TaskStore:
    """Durable task aggregates with application-owned transition checks."""

    def __init__(self, path: str | Path) -> None:
        self.database_url = str(path) if str(path).startswith(("postgres://", "postgresql://")) else ""
        self.is_postgres = bool(self.database_url)
        self.schema = os.getenv("ORCHESTRATOR_SCHEMA", "investory_orchestrator")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.schema):
            raise ValueError("ORCHESTRATOR_SCHEMA must be a simple SQL identifier")
        self.path = Path("/postgres") if self.is_postgres else Path(path)
        if self.is_postgres:
            self._ensure_schema()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _ensure_schema(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')

    def _connect(self) -> Any:
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(
                self.database_url,
                options=f"-c search_path={self.schema}",
                row_factory=dict_row,
            )
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            if self.is_postgres:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    issue_number INTEGER,
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
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    UNIQUE(repository, issue_number)
                )"""
                )
            else:
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
                    repository TEXT NOT NULL DEFAULT 'spider-su/investory',
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "repository" not in columns:
                    connection.execute(
                        "ALTER TABLE tasks ADD COLUMN repository TEXT NOT NULL DEFAULT 'spider-su/investory'"
                    )
                if "priority" not in columns:
                    connection.execute(
                        "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tasks_status_created "
                "ON tasks(status, priority, created_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at DOUBLE PRECISION NOT NULL
                )""" if self.is_postgres else """CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_events_task_created "
                "ON task_events(task_id, created_at)"
            )
            if self.is_postgres:
                repository_config_table_exists = connection.execute(
                    "SELECT to_regclass('repository_configs') IS NOT NULL AS present"
                ).fetchone()["present"]
            else:
                repository_config_table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='repository_configs'"
                ).fetchone() is not None
            connection.execute(
                """CREATE TABLE IF NOT EXISTS repository_configs (
                    repository TEXT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    base_branch TEXT NOT NULL DEFAULT 'develop',
                    github_project_number INTEGER,
                    priority_field_name TEXT NOT NULL DEFAULT 'Priority',
                    notification_login TEXT NOT NULL DEFAULT '',
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )"""
                if self.is_postgres
                else """CREATE TABLE IF NOT EXISTS repository_configs (
                    repository TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    base_branch TEXT NOT NULL DEFAULT 'develop',
                    github_project_number INTEGER,
                    priority_field_name TEXT NOT NULL DEFAULT 'Priority',
                    notification_login TEXT NOT NULL DEFAULT '',
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            if not repository_config_table_exists:
                self._execute(
                    connection,
                    """INSERT INTO repository_configs
                    (repository, enabled, base_branch, github_project_number,
                     priority_field_name, notification_login, poll_interval_seconds,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        os.getenv("GITHUB_REPOSITORY", "spider-su/investory"),
                        True,
                        os.getenv("BASE_BRANCH", "develop"),
                        None,
                        os.getenv("GITHUB_PRIORITY_FIELD", "Priority"),
                        os.getenv("GITHUB_NOTIFY_LOGIN", "spider-su"),
                        int(os.getenv("QUEUE_POLL_SECONDS", "30")),
                        time.time(),
                        time.time(),
                    ),
                )

    @staticmethod
    def _task(row: Any) -> Task:
        values = dict(row)
        values["status"] = TaskStatus(values["status"])
        if isinstance(values["metadata"], str):
            values["metadata"] = json.loads(values["metadata"])
        values.setdefault("repository", "spider-su/investory")
        values.setdefault("priority", 0)
        return Task(**values)

    def create(
        self,
        *,
        title: str,
        body: str = "",
        issue_number: int | None = None,
        source: str = "prompt",
        metadata: dict[str, Any] | None = None,
        repository: str | None = None,
        priority: int = 0,
    ) -> Task:
        repository = repository or os.getenv(
            "GITHUB_REPOSITORY", "spider-su/investory"
        )
        if issue_number is not None:
            existing = self.get(
                f"{repository}#{issue_number}"
                if self.is_postgres
                else str(issue_number)
            )
            if existing is not None:
                return existing
        now = time.time()
        if issue_number is None:
            task_id = uuid.uuid4().hex[:12]
        elif self.is_postgres:
            task_id = f"{repository}#{issue_number}"
        else:
            task_id = str(issue_number)
        metadata_value: Any = json.dumps(metadata or {})
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            metadata_value = Jsonb(metadata or {})
        with self._connection() as connection:
            self._execute(
                connection,
                """INSERT INTO tasks
                (task_id, source, repository, issue_number, title, body, status,
                 metadata, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, source, repository, issue_number, title, body,
                 TaskStatus.QUEUED.value, metadata_value, priority, now, now),
            )
            self._execute(
                connection,
                "INSERT INTO task_events "
                "(event_id, task_id, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, None, TaskStatus.QUEUED.value, "created", now),
            )
            row = self._execute(
                connection, "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.is_postgres else statement

    def _execute(
        self,
        connection: Any,
        statement: str,
        params: tuple[Any, ...] = (),
    ) -> Any:
        return connection.execute(self._sql(statement), params)

    def get(self, task_id: str) -> Task | None:
        with self._connection() as connection:
            if self.is_postgres:
                repository = os.getenv("GITHUB_REPOSITORY", "spider-su/investory")
                issue_number = int(task_id) if task_id.isdigit() else None
                task_id_value = task_id
                if "#" in task_id:
                    repository, issue_part = task_id.rsplit("#", 1)
                    issue_number = int(issue_part)
                row = self._execute(
                    connection,
                    "SELECT * FROM tasks WHERE task_id=? OR "
                    "(issue_number=? AND repository=?)",
                    (task_id_value, issue_number, repository),
                ).fetchone()
            else:
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
                rows = self._execute(
                    connection,
                    f"SELECT * FROM tasks WHERE status IN ({marks}) "
                    "ORDER BY priority DESC, created_at", tuple(values),
                ).fetchall()
            else:
                rows = self._execute(
                    connection,
                    "SELECT * FROM tasks ORDER BY priority DESC, created_at"
                ).fetchall()
        return [self._task(row) for row in rows]

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT event_id, task_id, from_status, to_status, detail, created_at "
                "FROM task_events WHERE task_id=? ORDER BY created_at, event_id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_repositories(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT * FROM repository_configs ORDER BY repository",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_repository(self, repository: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._execute(
                connection,
                "SELECT * FROM repository_configs WHERE repository=?",
                (repository,),
            ).fetchone()
        return dict(row) if row else None

    def save_repository(self, config: dict[str, Any]) -> dict[str, Any]:
        repository = config["repository"]
        now = time.time()
        values = (
            repository,
            config.get("enabled", True),
            config.get("base_branch", "develop"),
            config.get("github_project_number"),
            config.get("priority_field_name", "Priority"),
            config.get("notification_login", ""),
            config.get("poll_interval_seconds", 60),
            now,
            now,
        )
        with self._connection() as connection:
            self._execute(
                connection,
                """INSERT INTO repository_configs
                (repository, enabled, base_branch, github_project_number,
                 priority_field_name, notification_login, poll_interval_seconds,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository) DO UPDATE SET
                  enabled=excluded.enabled, base_branch=excluded.base_branch,
                  github_project_number=excluded.github_project_number,
                  priority_field_name=excluded.priority_field_name,
                  notification_login=excluded.notification_login,
                  poll_interval_seconds=excluded.poll_interval_seconds,
                  updated_at=excluded.updated_at""",
                values,
            )
            row = self._execute(
                connection,
                "SELECT * FROM repository_configs WHERE repository=?",
                (repository,),
            ).fetchone()
        return dict(row)

    def delete_repository(self, repository: str) -> bool:
        with self._connection() as connection:
            cursor = self._execute(
                connection,
                "DELETE FROM repository_configs WHERE repository=?",
                (repository,),
            )
        return cursor.rowcount > 0

    def statistics(self) -> dict[str, Any]:
        with self._connection() as connection:
            counts = self._execute(
                connection,
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status",
            ).fetchall()
            recent = self._execute(
                connection,
                "SELECT COUNT(*) AS count FROM tasks WHERE created_at >= ?",
                (time.time() - 7 * 24 * 60 * 60,),
            ).fetchone()
        return {
            "total": sum(row["count"] for row in counts),
            "last_7_days": recent["count"],
            "by_status": {row["status"]: row["count"] for row in counts},
        }

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
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            row = self._execute(
                connection,
                "SELECT * FROM tasks WHERE task_id=?" + (" FOR UPDATE" if self.is_postgres else ""),
                (task_id,),
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
                    else (
                        row["metadata"]
                        if metadata_value is None
                        and isinstance(row["metadata"], dict)
                        else json.loads(metadata_value or row["metadata"])
                    )
                )
                ci_status = updates.get("ci_status", row["ci_status"])
                missing = []
                if ci_status != "green":
                    missing.append("green CI")
                if (
                    not updates.get("pr_number", row["pr_number"])
                    or not updates.get("pr_url", row["pr_url"])
                ):
                    missing.append("pull request")
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
            if status == TaskStatus.COMPLETED:
                metadata_value = updates.get("metadata")
                metadata = (
                    metadata_value
                    if isinstance(metadata_value, dict)
                    else (
                        row["metadata"]
                        if metadata_value is None and isinstance(row["metadata"], dict)
                        else json.loads(metadata_value or row["metadata"])
                    )
                )
                completion_value = metadata.get("completion", {})
                completion = (
                    completion_value if isinstance(completion_value, dict) else {}
                )
                required = (
                    "source", "merge_commit_sha", "merged_at", "merged_by",
                    "base_branch", "post_merge_ci_status", "issue_closed",
                )
                missing = [
                    key for key in required
                    if key not in completion
                    or completion[key] is None
                    or completion[key] == ""
                ]
                if completion.get("source") != "human_merge":
                    missing.append("valid merge source")
                if (
                    not updates.get("pr_number", row["pr_number"])
                    or not updates.get("pr_url", row["pr_url"])
                ):
                    missing.append("recorded pull request")
                elif completion.get("pr_number") != updates.get("pr_number", row["pr_number"]):
                    missing.append("matching pull request evidence")
                if not re.fullmatch(r"[0-9a-f]{40}", str(completion.get("merge_commit_sha", ""))):
                    missing.append("valid merge commit SHA")
                if row["issue_number"] is not None and completion.get("issue_closed") is not True:
                    missing.append("closed linked issue")
                if updates.get("ci_status", row["ci_status"]) != "green":
                    missing.append("green post-merge CI")
                if completion.get("post_merge_ci_status") != "success":
                    missing.append("successful post-merge CI evidence")
                if missing:
                    raise ValueError(
                        "Cannot mark task COMPLETED without " + ", ".join(missing)
                    )
            serialized = {}
            for key, value in updates.items():
                if key == "metadata":
                    if self.is_postgres:
                        from psycopg.types.json import Jsonb

                        value = Jsonb(value)
                    else:
                        value = json.dumps(value)
                serialized[key] = value
            serialized["status"] = status.value
            serialized["updated_at"] = time.time()
            assignments = ", ".join(f"{key}=?" for key in serialized)
            self._execute(
                connection,
                f"UPDATE tasks SET {assignments} WHERE task_id=?",
                (*serialized.values(), task_id),
            )
            now = serialized["updated_at"]
            self._execute(
                connection,
                "INSERT INTO task_events "
                "(event_id, task_id, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    task_id,
                    current.value,
                    status.value,
                    (
                        f"completed:{metadata['completion']['source']}:"
                        f"pr#{metadata['completion']['pr_number']}:"
                        f"{metadata['completion']['merge_commit_sha']}"
                        if status == TaskStatus.COMPLETED
                        else "transition"
                    ),
                    now,
                ),
            )
            row = self._execute(
                connection,
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)
