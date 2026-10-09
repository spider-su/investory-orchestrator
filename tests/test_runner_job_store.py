from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.tasks import JobStatus, TaskStore
from app.runner_job_worker import run_job


def job_spec(task_id: str, *, capabilities: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "kind": "IMPLEMENT",
        "repository": "owner/repository",
        "base_branch": "develop",
        "task_branch": "agent/" + task_id.replace("#", "-"),
        "base_sha": "a" * 40,
        "expected_head_sha": "",
        "prompt": "Implement the task and validate it.",
        "config_snapshot": {"max_repairs": 3, "validation_adapter": "devcontainer_script"},
        "required_capabilities": capabilities or ["codex", "git", "build"],
    }


class RunnerJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")
        self.task = self.store.create(
            source="direct_prompt", title="Fixture task", body="Acceptance criteria",
            repository="owner/repository",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def register(self, runner_id: str = "runner-1", **kwargs: object) -> None:
        self.store.register_runner(
            runner_id,
            capabilities={"codex", "git", "build", "review"},
            **kwargs,
        )

    def enqueue(self, task_id: str | None = None, *, job_id: str | None = None) -> dict:
        return self.store.enqueue_job(
            job_spec(task_id or self.task.task_id), job_id=job_id or uuid.uuid4().hex,
        )

    def test_job_submission_is_idempotent_and_context_is_immutable(self) -> None:
        job_id = uuid.uuid4().hex
        spec = job_spec(self.task.task_id)
        first = self.store.enqueue_job(spec, job_id=job_id)
        duplicate = self.store.enqueue_job(spec, job_id=job_id)
        self.assertEqual(first["job_id"], duplicate["job_id"])
        changed = {**spec, "prompt": "A different prompt"}
        with self.assertRaisesRegex(ValueError, "different immutable context"):
            self.store.enqueue_job(changed, job_id=job_id)

    def test_job_configuration_rejects_secret_named_fields(self) -> None:
        spec = job_spec(self.task.task_id)
        spec["config_snapshot"] = {"github": {"access_token": "must-not-be-stored"}}
        with self.assertRaisesRegex(ValueError, "must not contain secrets"):
            self.store.enqueue_job(spec)

    def test_unknown_task_and_invalid_job_contract_are_rejected(self) -> None:
        spec = job_spec("not-a-task")
        with self.assertRaisesRegex(KeyError, "Unknown task"):
            self.store.enqueue_job(spec)
        spec = job_spec(self.task.task_id)
        spec["kind"] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "invalid job kind"):
            self.store.enqueue_job(spec)

    def test_claim_requires_capabilities_and_respects_capacity(self) -> None:
        self.register(max_codex_processes=1)
        task2 = self.store.create(
            source="direct_prompt", title="Second", body="Criteria",
            repository="owner/repository",
        )
        first = self.enqueue()
        second = self.store.enqueue_job(job_spec(task2.task_id))
        claimed = self.store.claim_job("runner-1")
        self.assertEqual(claimed["job_id"], first["job_id"])
        self.assertEqual(claimed["status"], JobStatus.RUNNING.value)
        self.assertIsNone(self.store.claim_job("runner-1"))
        self.assertEqual(self.store.get_job(second["job_id"])["status"], JobStatus.PENDING.value)

    def test_claim_does_not_run_incompatible_jobs(self) -> None:
        self.store.register_runner("runner-1", capabilities={"git"})
        self.enqueue()
        self.assertIsNone(self.store.claim_job("runner-1"))

    def test_codex_and_build_capacity_are_counted_independently(self) -> None:
        self.register(max_codex_processes=1, max_builds=1)
        second_task = self.store.create(
            source="direct_prompt", title="Build only", body="Criteria",
            repository="owner/repository",
        )
        codex_job = self.store.enqueue_job(
            job_spec(self.task.task_id, capabilities=["codex", "git"]),
        )
        build_job = self.store.enqueue_job(
            job_spec(second_task.task_id, capabilities=["build", "git"]),
        )
        first = self.store.claim_job("runner-1")
        second = self.store.claim_job("runner-1")
        self.assertEqual(first["job_id"], codex_job["job_id"])
        self.assertEqual(second["job_id"], build_job["job_id"])

    def test_concurrent_claimers_never_receive_the_same_job(self) -> None:
        self.register("runner-1", max_codex_processes=1)
        self.register("runner-2", max_codex_processes=1)
        self.enqueue()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.store.claim_job, ["runner-1", "runner-2"]))
        claimed = [job for job in results if job is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(self.store.list_job_attempts(claimed[0]["job_id"])), 1)

    def test_task_cannot_have_two_running_jobs(self) -> None:
        self.register("runner-1", max_codex_processes=2)
        self.register("runner-2", max_codex_processes=2)
        self.enqueue()
        self.enqueue()
        first = self.store.claim_job("runner-1")
        second = self.store.claim_job("runner-2")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_same_repository_branch_cannot_have_two_running_jobs(self) -> None:
        self.register("runner-1", max_codex_processes=2)
        self.register("runner-2", max_codex_processes=2)
        second_task = self.store.create(
            source="direct_prompt", title="Second", body="Criteria",
            repository="owner/repository",
        )
        first_spec = job_spec(self.task.task_id)
        first_spec["task_branch"] = "agent/shared-branch"
        second_spec = job_spec(second_task.task_id)
        second_spec["task_branch"] = "agent/shared-branch"
        self.store.enqueue_job(first_spec)
        self.store.enqueue_job(second_spec)
        self.assertIsNotNone(self.store.claim_job("runner-1"))
        self.assertIsNone(self.store.claim_job("runner-2"))

    def test_stale_runner_cannot_claim_jobs(self) -> None:
        self.register()
        job = self.enqueue()
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE runners SET last_heartbeat_at=? WHERE runner_id=?",
                (time.time() - 181, "runner-1"),
            )
        self.assertIsNone(self.store.claim_job("runner-1", lease_seconds=10))
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JobStatus.PENDING.value)
        with self.store._connection() as connection:
            runner = connection.execute(
                "SELECT status FROM runners WHERE runner_id='runner-1'",
            ).fetchone()
        self.assertEqual(runner["status"], "offline")

    def test_heartbeat_and_completion_are_fenced_by_attempt_id(self) -> None:
        self.register()
        job = self.enqueue()
        claimed = self.store.claim_job("runner-1")
        attempt_id = claimed["current_attempt_id"]
        self.assertFalse(self.store.heartbeat_job(
            job["job_id"], "runner-1", "stale-attempt-id",
        ))
        self.assertFalse(self.store.complete_job(
            job["job_id"], "runner-1", "stale-attempt-id", result={"ok": True},
        ))
        self.assertTrue(self.store.heartbeat_job(job["job_id"], "runner-1", attempt_id))
        self.assertTrue(self.store.complete_job(
            job["job_id"], "runner-1", attempt_id, result={"head_sha": "b" * 40},
        ))
        saved = self.store.get_job(job["job_id"])
        self.assertEqual(saved["status"], JobStatus.SUCCEEDED.value)
        self.assertEqual(saved["result"], {"head_sha": "b" * 40})

    def test_expired_lease_becomes_uncertain_and_requires_proven_stop(self) -> None:
        self.register()
        job = self.enqueue()
        claimed = self.store.claim_job("runner-1", lease_seconds=10)
        attempt_id = claimed["current_attempt_id"]
        expired = self.store.mark_expired_jobs_uncertain(
            now=claimed["lease_expires_at"] + 1,
        )
        self.assertEqual(expired, [job["job_id"]])
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JobStatus.UNCERTAIN.value)
        self.assertFalse(self.store.heartbeat_job(job["job_id"], "runner-1", attempt_id))
        self.assertFalse(self.store.complete_job(
            job["job_id"], "runner-1", attempt_id, result={"ok": True},
        ))
        with self.assertRaisesRegex(ValueError, "requires proof"):
            self.store.safely_requeue_uncertain_job(
                job["job_id"], attempt_id, previous_process_stopped=False,
            )
        self.assertTrue(self.store.safely_requeue_uncertain_job(
            job["job_id"], attempt_id, previous_process_stopped=True,
        ))
        second = self.store.claim_job("runner-1")
        self.assertNotEqual(second["current_attempt_id"], attempt_id)
        self.assertEqual(len(self.store.list_job_attempts(job["job_id"])), 2)

    def test_completion_requires_exactly_one_result_or_error(self) -> None:
        self.register()
        job = self.enqueue()
        claimed = self.store.claim_job("runner-1")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.store.complete_job(
                job["job_id"], "runner-1", claimed["current_attempt_id"],
            )


@unittest.skipUnless(
    os.getenv("TEST_POSTGRES_URL") and os.getenv("TEST_POSTGRES_SCHEMA"),
    "set TEST_POSTGRES_URL and TEST_POSTGRES_SCHEMA to the dedicated dev test database/schema",
)
class PostgreSqlRunnerJobTests(unittest.TestCase):
    def _clear_test_tables(self) -> None:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_URL"]) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def setUp(self) -> None:
        schema_prefix = os.environ["TEST_POSTGRES_SCHEMA"]
        if not re.fullmatch(r"orchestrator_test_[0-9]{8}_[a-f0-9]{8}", schema_prefix):
            raise ValueError(
                "TEST_POSTGRES_SCHEMA must be an isolated "
                "orchestrator_test_YYYYMMDD_<run-id> schema prefix"
            )
        self.schema = f"{schema_prefix}_{uuid.uuid4().hex[:8]}"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_schema = os.environ.get("ORCHESTRATOR_SCHEMA")
        os.environ["ORCHESTRATOR_SCHEMA"] = self.schema
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_URL"]) as connection:
            connection.execute(f'CREATE SCHEMA "{self.schema}"')
        self.store = TaskStore(os.environ["TEST_POSTGRES_URL"])
        self.task = self.store.create(
            source="direct_prompt", title="Postgres fixture", body="Criteria",
            repository="owner/repository",
        )

    def tearDown(self) -> None:
        self._clear_test_tables()
        if self.previous_schema is None:
            os.environ.pop("ORCHESTRATOR_SCHEMA", None)
        else:
            os.environ["ORCHESTRATOR_SCHEMA"] = self.previous_schema
        self.temp_dir.cleanup()

    def test_postgres_claim_is_atomic_and_attempt_is_persisted(self) -> None:
        self.store.register_runner("runner-1", capabilities={"codex", "git", "build"})
        self.store.register_runner("runner-2", capabilities={"codex", "git", "build"})
        job = self.store.enqueue_job(job_spec(self.task.task_id))
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(self.store.claim_job, ["runner-1", "runner-2"]))
        values = [item for item in claimed if item is not None]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["job_id"], job["job_id"])
        self.assertEqual(len(self.store.list_job_attempts(job["job_id"])), 1)

    def test_postgres_expired_attempt_is_fenced_until_stop_is_proven(self) -> None:
        self.store.register_runner(
            "runner-1", capabilities={"codex", "git", "build"},
        )
        job = self.store.enqueue_job(job_spec(self.task.task_id))
        first = self.store.claim_job("runner-1", lease_seconds=10)
        first_attempt = first["current_attempt_id"]

        expired = self.store.mark_expired_jobs_uncertain(
            now=first["lease_expires_at"] + 1,
        )

        self.assertEqual(expired, [job["job_id"]])
        self.assertFalse(self.store.heartbeat_job(
            job["job_id"], "runner-1", first_attempt,
        ))
        self.assertFalse(self.store.complete_job(
            job["job_id"], "runner-1", first_attempt, result={"ok": True},
        ))
        with self.assertRaisesRegex(ValueError, "requires proof"):
            self.store.safely_requeue_uncertain_job(
                job["job_id"], first_attempt, previous_process_stopped=False,
            )

        self.assertTrue(self.store.safely_requeue_uncertain_job(
            job["job_id"], first_attempt, previous_process_stopped=True,
        ))
        second = self.store.claim_job("runner-1")
        self.assertNotEqual(second["current_attempt_id"], first_attempt)
        self.assertFalse(self.store.complete_job(
            job["job_id"], "runner-1", first_attempt, result={"ok": True},
        ))
        self.assertEqual(len(self.store.list_job_attempts(job["job_id"])), 2)

    def test_postgres_submission_is_idempotent_and_branch_is_serialized(self) -> None:
        self.store.register_runner(
            "runner-1", capabilities={"codex", "git", "build"},
        )
        first_spec = job_spec(self.task.task_id)
        job_id = uuid.uuid4().hex
        first = self.store.enqueue_job(first_spec, job_id=job_id)
        duplicate = self.store.enqueue_job(first_spec, job_id=job_id)
        self.assertEqual(first["job_id"], duplicate["job_id"])

        conflicting = {**first_spec, "prompt": "Different immutable prompt"}
        with self.assertRaisesRegex(ValueError, "different immutable context"):
            self.store.enqueue_job(conflicting, job_id=job_id)

        second_task = self.store.create(
            source="direct_prompt", title="Same branch", body="Criteria",
            repository="owner/repository",
        )
        second_spec = job_spec(second_task.task_id)
        second_spec["task_branch"] = first_spec["task_branch"]
        second = self.store.enqueue_job(second_spec)

        claimed = self.store.claim_job("runner-1")
        self.assertEqual(claimed["job_id"], first["job_id"])
        self.assertIsNone(self.store.claim_job("runner-1"))
        self.assertEqual(
            self.store.get_job(second["job_id"])["status"],
            JobStatus.PENDING.value,
        )

    def test_postgres_runner_executes_and_persists_result_from_outside_repo(self) -> None:
        self.store.register_runner(
            "runner-1", capabilities={"codex", "git", "build"},
        )
        job = self.store.enqueue_job(job_spec(self.task.task_id))
        claimed = self.store.claim_job("runner-1")
        fake_python = Path(self.temp_dir.name) / "fake-python"
        fake_python.write_text(
            "#!/bin/sh\n"
            "python3 -c 'import json,os; "
            "json.dump({\"status\":\"fixture-completed\"}, "
            "open(os.environ[\"RUNNER_JOB_RESULT_PATH\"], \"w\"))'\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        result_dir = Path(self.temp_dir.name) / "results"
        with patch.dict(os.environ, {
            "DATABASE_URL": os.environ["TEST_POSTGRES_URL"],
            "ORCHESTRATOR_SCHEMA": self.schema,
            "RUNNER_TASK_PYTHON": str(fake_python),
            "RUNNER_RESULT_DIR": str(result_dir),
            "RUNNER_RUNS_DIR": str(Path(self.temp_dir.name) / "runs"),
            "RUNNER_WORKSPACES_DIR": str(Path(self.temp_dir.name) / "workspaces"),
            "RUNNER_JOB_LEASE_SECONDS": "30",
            "RUNNER_HEARTBEAT_SECONDS": "5",
        }, clear=False):
            result = run_job(job["job_id"], claimed["current_attempt_id"])

        self.assertEqual(result, 0)
        saved = self.store.get_job(job["job_id"])
        self.assertEqual(saved["status"], JobStatus.SUCCEEDED.value)
        self.assertEqual(
            saved["result"]["worker_result"],
            {"status": "fixture-completed"},
        )

    def test_postgres_task_cannot_have_two_running_jobs(self) -> None:
        self.store.register_runner(
            "runner-1", capabilities={"codex", "git", "build", "review"},
        )
        first = self.store.enqueue_job(
            job_spec(self.task.task_id), job_id=uuid.uuid4().hex,
        )
        repair_spec = {**job_spec(self.task.task_id), "kind": "REPAIR"}
        second = self.store.enqueue_job(
            repair_spec, job_id=uuid.uuid4().hex,
        )

        claimed = self.store.claim_job("runner-1")

        self.assertEqual(claimed["job_id"], first["job_id"])
        self.assertIsNone(self.store.claim_job("runner-1"))
        self.assertEqual(
            self.store.get_job(second["job_id"])["status"],
            JobStatus.PENDING.value,
        )
