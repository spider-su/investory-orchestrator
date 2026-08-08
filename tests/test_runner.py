from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.test_runner import (
    run_validation,
    start_environment,
    stop_environment,
)


class TestRunnerTests(unittest.TestCase):
    workspace = Path("/tmp/issue-42")

    @patch("app.test_runner._run", return_value=(0, "ready"))
    def test_start_environment_reports_success(self, run_mock) -> None:
        result = start_environment(self.workspace, 42)

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "ready")
        run_mock.assert_called_once_with(
            ["bash", "scripts/agent-devcontainer.sh", "up", "42"],
            workspace=self.workspace,
            timeout=1800,
        )

    @patch("app.test_runner._run", return_value=(2, "validation failed"))
    def test_validation_reports_project_failure(self, run_mock) -> None:
        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "project_validation_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 2)
        run_mock.assert_called_once_with(
            ["bash", "scripts/agent-devcontainer.sh", "validate", "42"],
            workspace=self.workspace,
            timeout=1800,
        )

    @patch("app.test_runner._run", return_value=(127, "missing command"))
    def test_stop_environment_reports_environment_failure(self, run_mock) -> None:
        result = stop_environment(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 127)
        run_mock.assert_called_once_with(
            ["bash", "scripts/agent-devcontainer.sh", "down", "42"],
            workspace=self.workspace,
            timeout=300,
        )


if __name__ == "__main__":
    unittest.main()
