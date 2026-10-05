from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.dashboard import create_app
from app.tasks import TaskStore


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "DASHBOARD_API_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "spider-su/investory",
            },
        )
        self.environment.start()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")
        self.client = TestClient(create_app(self.store))
        self.headers = {"Authorization": "Bearer test-token"}

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp_dir.cleanup()

    def test_dashboard_health_is_public_but_data_requires_token(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/tasks", headers=self.headers).json(), [])

    def test_repository_crud_task_events_and_stats(self) -> None:
        response = self.client.put(
            "/api/repositories",
            headers=self.headers,
            json={
                "repository": "spider-su/investory",
                "base_branch": "develop",
                "notification_login": "spider-su",
                "github_project_number": 3,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["base_branch"], "develop")
        self.assertEqual(len(self.client.get("/api/repositories", headers=self.headers).json()), 1)

        task = self.store.create(title="Dashboard task", issue_number=21)
        self.assertEqual(
            self.client.get(f"/api/tasks/{task.task_id}/events", headers=self.headers).json()[0]["detail"],
            "created",
        )
        self.assertEqual(self.client.get("/api/stats", headers=self.headers).json()["total"], 1)

        deleted = self.client.delete(
            "/api/repositories/spider-su%2Finvestory", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"deleted": True})
        self.assertEqual(TaskStore(self.store.path).list_repositories(), [])

    def test_repository_validation_rejects_bad_values(self) -> None:
        response = self.client.put(
            "/api/repositories",
            headers=self.headers,
            json={"repository": "not-a-repository", "base_branch": "../unsafe"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
