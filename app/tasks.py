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
    RUNNING = "RUNNING"
    WAITING_CI = "WAITING_CI"
    READY = "READY"
    DONE = "DONE"
    BLOCKED = "BLOCKED"

    # Source compatibility for workflow code and historical call sites. These
    # aliases all persist one lifecycle state; phase belongs in metadata.
    PLANNING = "RUNNING"
    IMPLEMENTING = "RUNNING"
    VALIDATING = "RUNNING"
    REVIEWING = "RUNNING"
    PUBLISHING = "RUNNING"
    FINAL_REVIEW = "RUNNING"
    COMPLETED = "DONE"
    FAILED = "BLOCKED"


class TaskPhase(StrEnum):
    PREPARING = "preparing"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    PUBLISHING = "publishing"


class JobKind(StrEnum):
    IMPLEMENT = "IMPLEMENT"
    REVIEW = "REVIEW"
    REPAIR = "REPAIR"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


JOB_SECRET_KEY_PARTS = (
    "token", "secret", "password", "private_key", "authorization", "credential",
    "api_key",
)


TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.WAITING_CI, TaskStatus.READY, TaskStatus.DONE, TaskStatus.BLOCKED}),
    TaskStatus.WAITING_CI: frozenset({TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.BLOCKED}),
    TaskStatus.READY: frozenset({TaskStatus.COMPLETED, TaskStatus.BLOCKED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_CI, TaskStatus.READY, TaskStatus.DONE}),
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
                    metadata TEXT NOT NULL DEFAULT '{}',
                    repository TEXT NOT NULL DEFAULT 'spider-su/investory',
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(repository, issue_number)
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
                # Older SQLite databases constrained issue_number globally.
                # Rebuild once so separate repositories can contain the same
                # issue number while preserving task/event IDs and data.
                indexes = connection.execute("PRAGMA index_list(tasks)").fetchall()
                globally_unique = any(
                    row["unique"]
                    and [column["name"] for column in connection.execute(
                        f"PRAGMA index_info('{row['name']}')"
                    ).fetchall()] == ["issue_number"]
                    for row in indexes
                )
                if globally_unique:
                    connection.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
                    connection.execute(
                        """CREATE TABLE tasks (
                        task_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                        issue_number INTEGER, title TEXT NOT NULL, body TEXT NOT NULL,
                        status TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
                        branch TEXT NOT NULL DEFAULT '', pr_number INTEGER,
                        pr_url TEXT NOT NULL DEFAULT '',
                        ci_status TEXT NOT NULL DEFAULT 'not_started',
                        implementation_attempts INTEGER NOT NULL DEFAULT 0,
                        validation_attempts INTEGER NOT NULL DEFAULT 0,
                        ci_attempts INTEGER NOT NULL DEFAULT 0,
                        blocked_reason TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
                        repository TEXT NOT NULL DEFAULT 'spider-su/investory',
                        priority INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, UNIQUE(repository, issue_number))"""
                    )
                    legacy_columns = [
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(tasks_legacy)"
                        ).fetchall()
                    ]
                    column_list = ", ".join(legacy_columns)
                    connection.execute(
                        f"INSERT INTO tasks ({column_list}) "
                        f"SELECT {column_list} FROM tasks_legacy"
                    )
                    connection.execute("DROP TABLE tasks_legacy")
            legacy_statuses = self._execute(
                connection,
                "SELECT task_id, status, metadata FROM tasks WHERE status IN ("
                "'PLANNING', 'IMPLEMENTING', 'VALIDATING', 'REVIEWING', "
                "'PUBLISHING', 'FINAL_REVIEW', 'COMPLETED', 'FAILED')",
            ).fetchall()
            status_map = {
                "PLANNING": ("RUNNING", "preparing"),
                "IMPLEMENTING": ("RUNNING", "implementing"),
                "VALIDATING": ("RUNNING", "validating"),
                "REVIEWING": ("RUNNING", "reviewing"),
                "PUBLISHING": ("RUNNING", "publishing"),
                "FINAL_REVIEW": ("RUNNING", "reviewing"),
                "COMPLETED": ("DONE", "publishing"),
                "FAILED": ("BLOCKED", "repairing"),
            }
            for legacy in legacy_statuses:
                new_status, phase = status_map[legacy["status"]]
                metadata = self._decode_json_field(legacy["metadata"], {})
                metadata.setdefault("phase", phase)
                self._execute(
                    connection,
                    "UPDATE tasks SET status=?, metadata=? WHERE task_id=?",
                    (new_status, self._json_value(metadata), legacy["task_id"]),
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS task_activity (
                    activity_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    slack_sent_at DOUBLE PRECISION,
                    created_at DOUBLE PRECISION NOT NULL
                )""" if self.is_postgres else """CREATE TABLE IF NOT EXISTS task_activity (
                    activity_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    slack_sent_at REAL,
                    created_at REAL NOT NULL
                )"""
            )
            if self.is_postgres:
                connection.execute(
                    "ALTER TABLE task_activity ADD COLUMN IF NOT EXISTS slack_sent_at DOUBLE PRECISION"
                )
            else:
                activity_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(task_activity)"
                    ).fetchall()
                }
                if "slack_sent_at" not in activity_columns:
                    connection.execute(
                        "ALTER TABLE task_activity ADD COLUMN slack_sent_at REAL"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_activity_task_created "
                "ON task_activity(task_id, created_at, activity_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runners (
                    runner_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'offline',
                    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
                    max_codex_processes INTEGER NOT NULL DEFAULT 1,
                    max_builds INTEGER NOT NULL DEFAULT 1,
                    version TEXT NOT NULL DEFAULT '',
                    last_heartbeat_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION NOT NULL
                )""" if self.is_postgres else """CREATE TABLE IF NOT EXISTS runners (
                    runner_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'offline',
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    max_codex_processes INTEGER NOT NULL DEFAULT 1,
                    max_builds INTEGER NOT NULL DEFAULT 1,
                    version TEXT NOT NULL DEFAULT '',
                    last_heartbeat_at REAL,
                    updated_at REAL NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    task_branch TEXT NOT NULL,
                    base_sha TEXT NOT NULL DEFAULT '',
                    expected_head_sha TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    job_spec JSONB NOT NULL,
                    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
                    uses_codex BOOLEAN NOT NULL DEFAULT FALSE,
                    uses_build BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    runner_id TEXT,
                    current_attempt_id TEXT,
                    lease_expires_at DOUBLE PRECISION,
                    result JSONB,
                    error JSONB,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )""" if self.is_postgres else """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    task_branch TEXT NOT NULL,
                    base_sha TEXT NOT NULL DEFAULT '',
                    expected_head_sha TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    job_spec TEXT NOT NULL,
                    config_snapshot TEXT NOT NULL DEFAULT '{}',
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    uses_codex INTEGER NOT NULL DEFAULT 0,
                    uses_build INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    runner_id TEXT,
                    current_attempt_id TEXT,
                    lease_expires_at REAL,
                    result TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    runner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at DOUBLE PRECISION NOT NULL,
                    heartbeat_at DOUBLE PRECISION NOT NULL,
                    lease_expires_at DOUBLE PRECISION NOT NULL,
                    worker_pid INTEGER,
                    log_path TEXT NOT NULL DEFAULT '',
                    finished_at DOUBLE PRECISION,
                    result JSONB,
                    error JSONB,
                    UNIQUE(job_id, attempt_number)
                )""" if self.is_postgres else """CREATE TABLE IF NOT EXISTS job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    runner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    worker_pid INTEGER,
                    log_path TEXT NOT NULL DEFAULT '',
                    finished_at REAL,
                    result TEXT,
                    error TEXT,
                    UNIQUE(job_id, attempt_number)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_pending_priority "
                "ON jobs(status, priority DESC, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_runner_status "
                "ON jobs(runner_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS job_attempts_job_started "
                "ON job_attempts(job_id, started_at)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running_per_task "
                "ON jobs(task_id) WHERE status='running'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running_per_branch "
                "ON jobs(repository, task_branch) WHERE status='running'"
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS service_status (
                    name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at DOUBLE PRECISION NOT NULL
                )"""
                if self.is_postgres
                else """CREATE TABLE IF NOT EXISTS service_status (
                    name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
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
            existing = self.get(f"{repository}#{issue_number}")
            if existing is not None:
                return existing
        now = time.time()
        if issue_number is None:
            task_id = uuid.uuid4().hex[:12]
        elif not self.is_postgres and repository == os.getenv(
            "GITHUB_REPOSITORY", "spider-su/investory"
        ):
            # Preserve legacy dashboard URLs for the primary SQLite repository.
            task_id = str(issue_number)
        else:
            task_id = f"{repository}#{issue_number}"
        task_metadata = dict(metadata or {})
        task_metadata.setdefault("phase", TaskPhase.PREPARING.value)
        metadata_value: Any = json.dumps(task_metadata)
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            metadata_value = Jsonb(task_metadata)
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
            initial_metadata: Any = json.dumps({})
            if self.is_postgres:
                from psycopg.types.json import Jsonb

                initial_metadata = Jsonb({})
            self._execute(
                connection,
                "INSERT INTO task_activity "
                "(activity_id, task_id, actor, event_type, message, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, "orchestrator", "task_queued",
                 "Task queued for processing.", initial_metadata, now),
            )
            row = self._execute(
                connection, "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)

    def reserve_code_repair(
        self,
        task_id: str,
        *,
        fallback_limit: int,
    ) -> dict[str, int] | None:
        """Atomically reserve one task repair; return None when exhausted."""
        if fallback_limit < 0:
            raise ValueError("repair limit cannot be negative")
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            row = self._execute(
                connection,
                "SELECT metadata FROM tasks WHERE task_id=?"
                + (" FOR UPDATE" if self.is_postgres else ""),
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            metadata = self._decode_json_field(row["metadata"], {})
            budget = metadata.get("repair_budget")
            if budget is None:
                budget = {"limit": fallback_limit, "used": 0}
            if not isinstance(budget, dict):
                raise ValueError(f"Invalid repair budget for task {task_id}")
            limit, used = budget.get("limit", fallback_limit), budget.get("used", 0)
            if (
                isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
                or isinstance(used, bool) or not isinstance(used, int) or used < 0
            ):
                raise ValueError(f"Invalid repair budget for task {task_id}")
            if used >= limit:
                return None
            budget = {"limit": limit, "used": used + 1}
            metadata["repair_budget"] = budget
            self._execute(
                connection,
                "UPDATE tasks SET metadata=?, updated_at=? WHERE task_id=?",
                (self._json_value(metadata), time.time(), task_id),
            )
            created_at = time.time()
            self._execute(
                connection,
                "INSERT INTO task_activity "
                "(activity_id, task_id, actor, event_type, message, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex, task_id, "orchestrator", "repair_reserved",
                    f"Code repair budget {budget['used']}/{limit} reserved.",
                    self._json_value(budget), created_at,
                ),
            )
        return budget

    def release_code_repair(self, task_id: str) -> dict[str, int]:
        """Refund a reservation when a repair invocation produced no candidate."""
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            row = self._execute(
                connection,
                "SELECT metadata FROM tasks WHERE task_id=?"
                + (" FOR UPDATE" if self.is_postgres else ""),
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            metadata = self._decode_json_field(row["metadata"], {})
            budget = metadata.get("repair_budget") or {"limit": 0, "used": 0}
            if (
                not isinstance(budget, dict)
                or isinstance(budget.get("used"), bool)
                or not isinstance(budget.get("used"), int)
            ):
                raise ValueError(f"Invalid repair budget for task {task_id}")
            limit = budget.get("limit", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError(f"Invalid repair budget for task {task_id}")
            budget = {"limit": limit, "used": max(0, budget["used"] - 1)}
            metadata["repair_budget"] = budget
            self._execute(
                connection,
                "UPDATE tasks SET metadata=?, updated_at=? WHERE task_id=?",
                (self._json_value(metadata), time.time(), task_id),
            )
        return budget

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.is_postgres else statement

    def _execute(
        self,
        connection: Any,
        statement: str,
        params: tuple[Any, ...] = (),
    ) -> Any:
        return connection.execute(self._sql(statement), params)

    def _json_value(self, value: Any) -> Any:
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            return Jsonb(value)
        return json.dumps(value, separators=(",", ":"))

    @staticmethod
    def _decode_json_field(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        return value

    def _job_row(self, row: Any) -> dict[str, Any]:
        job = dict(row)
        for field, default in (
            ("job_spec", {}),
            ("config_snapshot", {}),
            ("required_capabilities", []),
            ("result", None),
            ("error", None),
        ):
            job[field] = self._decode_json_field(job.get(field), default)
        return job

    @staticmethod
    def _contains_secret_key(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if any(part in normalized for part in JOB_SECRET_KEY_PARTS):
                    return True
                if TaskStore._contains_secret_key(nested):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(TaskStore._contains_secret_key(item) for item in value)
        return False

    def register_runner(
        self,
        runner_id: str,
        *,
        capabilities: set[str] | list[str] | tuple[str, ...],
        max_codex_processes: int = 1,
        max_builds: int = 1,
        version: str = "",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", runner_id):
            raise ValueError("invalid runner identity")
        if max_codex_processes < 1 or max_builds < 1:
            raise ValueError("runner capacities must be positive")
        normalized_capabilities = sorted({str(value) for value in capabilities})
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) for value in normalized_capabilities):
            raise ValueError("invalid runner capability")
        now = time.time()
        with self._connection() as connection:
            self._execute(
                connection,
                """INSERT INTO runners
                (runner_id, status, capabilities, max_codex_processes, max_builds,
                 version, last_heartbeat_at, updated_at)
                VALUES (?, 'online', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                  status='online', capabilities=excluded.capabilities,
                  max_codex_processes=excluded.max_codex_processes,
                  max_builds=excluded.max_builds, version=excluded.version,
                  last_heartbeat_at=excluded.last_heartbeat_at,
                  updated_at=excluded.updated_at""",
                (runner_id, self._json_value(normalized_capabilities),
                 max_codex_processes, max_builds, version[:160], now, now),
            )
            row = self._execute(
                connection, "SELECT * FROM runners WHERE runner_id=?", (runner_id,),
            ).fetchone()
        runner = dict(row)
        runner["capabilities"] = self._decode_json_field(runner["capabilities"], [])
        return runner

    def heartbeat_runner(self, runner_id: str, *, status: str = "online") -> bool:
        now = time.time()
        with self._connection() as connection:
            cursor = self._execute(
                connection,
                "UPDATE runners SET status=?, last_heartbeat_at=?, updated_at=? "
                "WHERE runner_id=?",
                (status, now, now, runner_id),
            )
        return cursor.rowcount == 1

    def list_runners(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection, "SELECT * FROM runners ORDER BY runner_id",
            ).fetchall()
        runners = []
        for row in rows:
            runner = dict(row)
            runner["capabilities"] = self._decode_json_field(
                runner.get("capabilities"), []
            )
            runners.append(runner)
        return runners

    def enqueue_job(
        self,
        spec: dict[str, Any],
        *,
        job_id: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        required = (
            "task_id", "kind", "repository", "base_branch", "task_branch",
            "prompt", "config_snapshot", "required_capabilities",
        )
        missing = [name for name in required if name not in spec]
        if missing:
            raise ValueError(f"job spec is missing fields: {', '.join(missing)}")
        if not isinstance(spec["kind"], str) or spec["kind"] not in {item.value for item in JobKind}:
            raise ValueError("invalid job kind")
        task_id = str(spec["task_id"])
        if self.get(task_id) is None:
            raise KeyError(f"Unknown task: {task_id}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(spec["repository"])):
            raise ValueError("invalid repository identity")
        for field in ("base_branch", "task_branch"):
            value = spec[field]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", value):
                raise ValueError(f"invalid {field}")
        for field in ("base_sha", "expected_head_sha"):
            value = spec.get(field)
            if value not in (None, "") and not re.fullmatch(r"[0-9a-f]{40}", str(value)):
                raise ValueError(f"invalid {field}")
        if not isinstance(spec["prompt"], str):
            raise ValueError("job prompt must be a string")
        if not isinstance(spec["config_snapshot"], dict):
            raise ValueError("job config_snapshot must be an object")
        if self._contains_secret_key(spec["config_snapshot"]):
            raise ValueError("job config_snapshot must not contain secrets")
        capabilities = spec["required_capabilities"]
        if not isinstance(capabilities, (list, tuple, set)) or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", item)
            for item in capabilities
        ):
            raise ValueError("required_capabilities must be a list of capability names")
        canonical_spec = json.loads(json.dumps(spec, sort_keys=True, separators=(",", ":")))
        job_id = job_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("job_id must be a 32-character lowercase hexadecimal ID")
        now = time.time()
        uses_codex = "codex" in capabilities
        uses_build = "build" in capabilities
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            existing = self._execute(
                connection, "SELECT * FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if existing is not None:
                saved = self._job_row(existing)
                if saved["job_spec"] != canonical_spec:
                    raise ValueError("job ID already exists with different immutable context")
                return saved
            self._execute(
                connection,
                """INSERT INTO jobs
                (job_id, task_id, kind, repository, base_branch, task_branch, base_sha,
                 expected_head_sha, prompt, job_spec, config_snapshot, required_capabilities,
                 uses_codex, uses_build, status, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (job_id, task_id, spec["kind"], spec["repository"], spec["base_branch"],
                 spec["task_branch"], spec.get("base_sha") or "",
                 spec.get("expected_head_sha") or "", spec["prompt"],
                 self._json_value(canonical_spec), self._json_value(spec["config_snapshot"]),
                 self._json_value(sorted(set(capabilities))), uses_codex, uses_build,
                 priority, now, now),
            )
            row = self._execute(
                connection, "SELECT * FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
        return self._job_row(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._execute(
                connection, "SELECT * FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, statuses: set[JobStatus] | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if statuses:
                values = [status.value for status in statuses]
                marks = ",".join("?" for _ in values)
                rows = self._execute(
                    connection,
                    f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY priority DESC, created_at",
                    tuple(values),
                ).fetchall()
            else:
                rows = self._execute(
                    connection, "SELECT * FROM jobs ORDER BY priority DESC, created_at",
                ).fetchall()
        return [self._job_row(row) for row in rows]

    def claim_job(
        self,
        runner_id: str,
        *,
        lease_seconds: int = 90,
    ) -> dict[str, Any] | None:
        if lease_seconds < 10:
            raise ValueError("job lease must be at least 10 seconds")
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            runner_query = "SELECT * FROM runners WHERE runner_id=?"
            if self.is_postgres:
                runner_query += " FOR UPDATE"
            runner = self._execute(connection, runner_query, (runner_id,)).fetchone()
            if runner is None or runner["status"] != "online":
                raise RuntimeError("runner must be registered and online before claiming jobs")
            stale_after = max(lease_seconds * 3, 180)
            if now - float(runner["last_heartbeat_at"] or 0) > stale_after:
                self._execute(
                    connection,
                    "UPDATE runners SET status='offline', updated_at=? WHERE runner_id=?",
                    (now, runner_id),
                )
                connection.commit()
                return None
            runner_capabilities = set(
                self._decode_json_field(runner["capabilities"], [])
            )
            pending_query = (
                "SELECT * FROM jobs WHERE status='pending' "
                "ORDER BY priority DESC, created_at LIMIT 50"
            )
            if self.is_postgres:
                pending_query = (
                    "SELECT * FROM jobs WHERE status='pending' "
                    "ORDER BY priority DESC, created_at LIMIT 50 FOR UPDATE SKIP LOCKED"
                )
            candidates = self._execute(connection, pending_query).fetchall()
            active = self._execute(
                connection,
                "SELECT uses_codex, uses_build FROM jobs WHERE runner_id=? AND status='running'",
                (runner_id,),
            ).fetchall()
            codex_in_use = sum(bool(row["uses_codex"]) for row in active)
            builds_in_use = sum(bool(row["uses_build"]) for row in active)
            for candidate in candidates:
                required = set(self._decode_json_field(candidate["required_capabilities"], []))
                if not required.issubset(runner_capabilities):
                    continue
                uses_codex = bool(candidate["uses_codex"])
                uses_build = bool(candidate["uses_build"])
                if uses_codex and codex_in_use >= runner["max_codex_processes"]:
                    continue
                if uses_build and builds_in_use >= runner["max_builds"]:
                    continue
                task_query = "SELECT task_id FROM tasks WHERE task_id=?"
                if self.is_postgres:
                    task_query += " FOR UPDATE"
                self._execute(connection, task_query, (candidate["task_id"],)).fetchone()
                active_for_task = self._execute(
                    connection,
                    "SELECT job_id FROM jobs WHERE status='running' AND "
                    "(task_id=? OR (repository=? AND task_branch=?)) LIMIT 1",
                    (candidate["task_id"], candidate["repository"], candidate["task_branch"]),
                ).fetchone()
                if active_for_task is not None:
                    continue
                attempt_id = uuid.uuid4().hex
                attempt_number = int(candidate["attempt_count"]) + 1
                lease_expires_at = now + lease_seconds
                updated = self._execute(
                    connection,
                    """UPDATE jobs SET status='running', attempt_count=?, runner_id=?,
                    current_attempt_id=?, lease_expires_at=?, updated_at=?
                    WHERE job_id=? AND status='pending'""",
                    (attempt_number, runner_id, attempt_id, lease_expires_at, now,
                     candidate["job_id"]),
                )
                if updated.rowcount != 1:
                    continue
                self._execute(
                    connection,
                    """INSERT INTO job_attempts
                    (attempt_id, job_id, attempt_number, runner_id, status, started_at,
                     heartbeat_at, lease_expires_at)
                    VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
                    (attempt_id, candidate["job_id"], attempt_number, runner_id, now, now,
                     lease_expires_at),
                )
                self._execute(
                    connection,
                    "UPDATE runners SET last_heartbeat_at=?, updated_at=? WHERE runner_id=?",
                    (now, now, runner_id),
                )
                row = self._execute(
                    connection, "SELECT * FROM jobs WHERE job_id=?", (candidate["job_id"],),
                ).fetchone()
                job = self._job_row(row)
                connection.commit()
                return job
            connection.commit()
            return None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat_job(
        self,
        job_id: str,
        runner_id: str,
        attempt_id: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        if lease_seconds < 10:
            raise ValueError("job lease must be at least 10 seconds")
        now = time.time()
        lease_expires_at = now + lease_seconds
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            updated = self._execute(
                connection,
                """UPDATE jobs SET lease_expires_at=?, updated_at=?
                WHERE job_id=? AND runner_id=? AND current_attempt_id=?
                  AND status='running' AND lease_expires_at>=?""",
                (lease_expires_at, now, job_id, runner_id, attempt_id, now),
            )
            if updated.rowcount != 1:
                return False
            self._execute(
                connection,
                """UPDATE job_attempts SET heartbeat_at=?, lease_expires_at=?
                WHERE attempt_id=? AND job_id=? AND runner_id=? AND status='running'""",
                (now, lease_expires_at, attempt_id, job_id, runner_id),
            )
            self._execute(
                connection,
                "UPDATE runners SET last_heartbeat_at=?, updated_at=? WHERE runner_id=?",
                (now, now, runner_id),
            )
        return True

    def record_job_process(
        self,
        job_id: str,
        runner_id: str,
        attempt_id: str,
        *,
        worker_pid: int,
        log_path: str,
    ) -> bool:
        if worker_pid < 1:
            raise ValueError("worker PID must be positive")
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            updated = self._execute(
                connection,
                """UPDATE job_attempts SET worker_pid=?, log_path=?
                WHERE attempt_id=? AND job_id=? AND runner_id=? AND status='running'
                  AND EXISTS (SELECT 1 FROM jobs WHERE job_id=? AND runner_id=?
                    AND current_attempt_id=? AND status='running' AND lease_expires_at>=?)""",
                (worker_pid, log_path, attempt_id, job_id, runner_id,
                 job_id, runner_id, attempt_id, now),
            )
        return updated.rowcount == 1

    def complete_job(
        self,
        job_id: str,
        runner_id: str,
        attempt_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        if (result is None) == (error is None):
            raise ValueError("provide exactly one of result or error")
        status = JobStatus.SUCCEEDED.value if error is None else JobStatus.FAILED.value
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            updated = self._execute(
                connection,
                """UPDATE jobs SET status=?, result=?, error=?, lease_expires_at=NULL,
                updated_at=? WHERE job_id=? AND runner_id=? AND current_attempt_id=?
                  AND status='running' AND lease_expires_at>=?""",
                (status, self._json_value(result) if result is not None else None,
                 self._json_value(error) if error is not None else None,
                 now, job_id, runner_id, attempt_id, now),
            )
            if updated.rowcount != 1:
                return False
            self._execute(
                connection,
                """UPDATE job_attempts SET status=?, result=?, error=?, finished_at=?,
                heartbeat_at=? WHERE attempt_id=? AND job_id=? AND runner_id=?
                  AND status='running'""",
                (status, self._json_value(result) if result is not None else None,
                 self._json_value(error) if error is not None else None,
                 now, now, attempt_id, job_id, runner_id),
            )
        return True

    def mark_expired_jobs_uncertain(self, *, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            query = (
                "SELECT job_id, current_attempt_id FROM jobs "
                "WHERE status='running' AND lease_expires_at<?"
            )
            if self.is_postgres:
                query += " FOR UPDATE SKIP LOCKED"
            rows = self._execute(connection, query, (cutoff,)).fetchall()
            expired: list[str] = []
            for row in rows:
                self._execute(
                    connection,
                    "UPDATE jobs SET status='uncertain', updated_at=? "
                    "WHERE job_id=? AND status='running' AND current_attempt_id=?",
                    (cutoff, row["job_id"], row["current_attempt_id"]),
                )
                if row["current_attempt_id"]:
                    self._execute(
                        connection,
                        "UPDATE job_attempts SET status='uncertain' WHERE attempt_id=? "
                        "AND status='running'",
                        (row["current_attempt_id"],),
                    )
                expired.append(row["job_id"])
            connection.commit()
            return expired
        finally:
            connection.close()

    def safely_requeue_uncertain_job(
        self,
        job_id: str,
        attempt_id: str,
        *,
        previous_process_stopped: bool,
    ) -> bool:
        if not previous_process_stopped:
            raise ValueError("uncertain job requeue requires proof the previous process stopped")
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN" if self.is_postgres else "BEGIN IMMEDIATE")
            updated = self._execute(
                connection,
                """UPDATE jobs SET status='pending', runner_id=NULL,
                current_attempt_id=NULL, lease_expires_at=NULL, result=NULL, error=NULL,
                updated_at=? WHERE job_id=? AND status='uncertain' AND current_attempt_id=?""",
                (now, job_id, attempt_id),
            )
        return updated.rowcount == 1

    def list_job_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
                (job_id,),
            ).fetchall()
        attempts = []
        for row in rows:
            attempt = dict(row)
            for field in ("result", "error"):
                attempt[field] = self._decode_json_field(attempt.get(field), None)
            attempts.append(attempt)
        return attempts

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
                if "#" in task_id:
                    repository, issue_part = task_id.rsplit("#", 1)
                    issue_number = int(issue_part)
                    row = connection.execute(
                        "SELECT * FROM tasks WHERE task_id=? OR "
                        "(issue_number=? AND repository=?)",
                        (task_id, issue_number, repository),
                    ).fetchone()
                else:
                    issue_number = int(task_id) if task_id.isdigit() else None
                    repository = os.getenv("GITHUB_REPOSITORY", "spider-su/investory")
                    row = connection.execute(
                        "SELECT * FROM tasks WHERE task_id=? OR "
                        "(issue_number=? AND repository=?)",
                        (task_id, issue_number, repository),
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

    def add_activity(
        self,
        task_id: str,
        *,
        actor: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.get(task_id) is None:
            raise KeyError(f"Unknown task: {task_id}")
        now = time.time()
        activity_metadata: Any = json.dumps(metadata or {})
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            activity_metadata = Jsonb(metadata or {})
        with self._connection() as connection:
            self._execute(
                connection,
                "INSERT INTO task_activity "
                "(activity_id, task_id, actor, event_type, message, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, actor, event_type, message,
                 activity_metadata, now),
            )

    def list_pending_activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT activity_id, task_id, actor, event_type, message, metadata, created_at "
                "FROM task_activity WHERE slack_sent_at IS NULL "
                "ORDER BY created_at, activity_id LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            if isinstance(entry["metadata"], str):
                entry["metadata"] = json.loads(entry["metadata"] or "{}")
            result.append(entry)
        return result

    def mark_activity_sent(self, activity_id: str) -> None:
        with self._connection() as connection:
            self._execute(
                connection,
                "UPDATE task_activity SET slack_sent_at=? WHERE activity_id=?",
                (time.time(), activity_id),
            )

    def list_activity(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT activity_id, task_id, actor, event_type, message, metadata, created_at "
                "FROM task_activity WHERE task_id=? ORDER BY created_at, activity_id",
                (task_id,),
            ).fetchall()
        activity = []
        for row in rows:
            entry = dict(row)
            if isinstance(entry["metadata"], str):
                entry["metadata"] = json.loads(entry["metadata"] or "{}")
            activity.append(entry)
        return activity

    def heartbeat_worker(self, task_id: str, owner: str, pid: int) -> None:
        now = time.time()
        values = {"lease_owner": owner, "worker_heartbeat_at": now, "worker_pid": pid}
        metadata_value: Any = json.dumps(values)
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            metadata_value = Jsonb(values)
        with self._connection() as connection:
            if self.is_postgres:
                self._execute(
                    connection,
                    "UPDATE tasks SET metadata=metadata || ?, updated_at=? WHERE task_id=?",
                    (metadata_value, now, task_id),
                )
            else:
                self._execute(
                    connection,
                    "UPDATE tasks SET metadata=json_patch(metadata, ?), updated_at=? "
                    "WHERE task_id=?",
                    (metadata_value, now, task_id),
                )

    def list_repositories(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT * FROM repository_configs ORDER BY repository",
            ).fetchall()
        return [dict(row) for row in rows]

    def set_service_status(
        self,
        name: str,
        status: str,
        detail: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        status_metadata: Any = json.dumps(metadata or {})
        if self.is_postgres:
            from psycopg.types.json import Jsonb

            status_metadata = Jsonb(metadata or {})
        with self._connection() as connection:
            self._execute(
                connection,
                """INSERT INTO service_status (name, status, detail, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET status=excluded.status,
                detail=excluded.detail, metadata=excluded.metadata,
                updated_at=excluded.updated_at""",
                (name, status, detail, status_metadata, now),
            )
        return {
            "name": name,
            "status": status,
            "detail": detail,
            "metadata": metadata or {},
            "updated_at": now,
        }

    def get_service_status(self, name: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._execute(
                connection,
                "SELECT * FROM service_status WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        if isinstance(value.get("metadata"), str):
            try:
                value["metadata"] = json.loads(value["metadata"])
            except json.JSONDecodeError:
                value["metadata"] = {}
        return value

    def list_service_status(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(
                connection,
                "SELECT * FROM service_status ORDER BY name",
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            if isinstance(value.get("metadata"), str):
                try:
                    value["metadata"] = json.loads(value["metadata"])
                except json.JSONDecodeError:
                    value["metadata"] = {}
            result.append(value)
        return result

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
                no_changes = completion.get("outcome") == "no_changes"
                required = (
                    (
                        "outcome", "summary", "issue_closed",
                        "validation_status", "review_status",
                    )
                    if no_changes
                    else (
                        "source", "merge_commit_sha", "merged_at", "merged_by",
                        "base_branch", "post_merge_ci_status", "issue_closed",
                    )
                )
                missing = [
                    key for key in required
                    if key not in completion
                    or completion[key] is None
                    or completion[key] == ""
                ]
                if no_changes:
                    if completion.get("validation_status") != "validation_success":
                        missing.append("successful final validation evidence")
                    if completion.get("review_status") != "approved":
                        missing.append("approved final review evidence")
                    if completion.get("issue_closed") is not False:
                        missing.append("open issue evidence for no-change outcome")
                else:
                    if completion.get("source") not in {"human_merge", "approved_review"}:
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
                        "completed:no_changes"
                        if status == TaskStatus.COMPLETED
                        and metadata.get("completion", {}).get("outcome") == "no_changes"
                        else (
                            f"completed:{metadata['completion']['source']}:"
                            f"pr#{metadata['completion']['pr_number']}:"
                            f"{metadata['completion']['merge_commit_sha']}"
                            if status == TaskStatus.COMPLETED
                            else "transition"
                        )
                    ),
                    now,
                ),
            )
            if status != current:
                activity_data = {
                    "from_status": current.value,
                    "to_status": status.value,
                }
                activity_metadata: Any = json.dumps(activity_data)
                if self.is_postgres:
                    from psycopg.types.json import Jsonb

                    activity_metadata = Jsonb(activity_data)
                self._execute(
                    connection,
                    "INSERT INTO task_activity "
                    "(activity_id, task_id, actor, event_type, message, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, task_id, "orchestrator", "workflow_status",
                     f"Task status changed to {status.value.replace('_', ' ').lower()}.",
                     activity_metadata, now),
                )
            row = self._execute(
                connection,
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)
