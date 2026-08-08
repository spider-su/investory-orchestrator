from __future__ import annotations

import unittest
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from app.test_runner import (
    MAX_OUTPUT_LENGTH,
    _RunResult,
    _run,
    run_validation,
    start_environment,
    stop_environment,
)


class TestRunnerTests(unittest.TestCase):
    workspace = Path("/tmp/issue-42")

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=(0, "ready"),
    )
    def test_start_environment_reports_success(self, action_mock) -> None:
        result = start_environment(self.workspace, 42)

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "ready")
        action_mock.assert_called_once_with(
            "up",
            workspace=self.workspace,
            issue_number=42,
            timeout=1800,
        )

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=(2, "validation failed"),
    )
    def test_validation_reports_project_failure(self, action_mock) -> None:
        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "project_validation_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 2)
        action_mock.assert_called_once_with(
            "validate",
            workspace=self.workspace,
            issue_number=42,
            timeout=1800,
        )

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=(127, "validation command missing"),
    )
    def test_validation_reports_environment_failure(self, run_mock) -> None:
        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 127)

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=(124, "validation timed out"),
    )
    def test_validation_timeout_reports_environment_failure(self, run_mock) -> None:
        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 124)

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=_RunResult(127, "project validation failed"),
    )
    def test_validation_child_exit_code_is_project_failure(self, run_mock) -> None:
        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "project_validation_failure")
        self.assertEqual(result["exit_code"], 127)

    @patch(
        "app.test_runner._run_devcontainer_action",
        return_value=(127, "missing command"),
    )
    def test_stop_environment_reports_environment_failure(self, action_mock) -> None:
        result = stop_environment(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 127)
        action_mock.assert_called_once_with(
            "down",
            workspace=self.workspace,
            issue_number=42,
            timeout=300,
        )

    def test_missing_devcontainer_script_is_environment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_validation(Path(directory), 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("script was not found", result["output"])

    @patch(
        "app.test_runner._run",
        return_value=_RunResult(1, "bash is unavailable"),
    )
    def test_unusable_bash_is_environment_failure(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = run_validation(workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Could not initialize bash", result["output"])
        run_mock.assert_called_once_with(
            ["bash", "-c", "exit 0"],
            workspace=workspace,
            timeout=30,
        )

    def test_run_limits_command_output(self) -> None:
        _, output = _run(
            [sys.executable, "-c", "print('x' * 200000)"],
            workspace=Path.cwd(),
            timeout=10,
        )

        self.assertLessEqual(len(output), MAX_OUTPUT_LENGTH)
        self.assertIn("output truncated", output)

    def test_run_terminates_timed_out_command(self) -> None:
        exit_code, output = _run(
            [
                sys.executable,
                "-c",
                "import time; print('x' * 200000, flush=True); time.sleep(30)",
            ],
            workspace=Path.cwd(),
            timeout=0.1,
        )

        self.assertEqual(exit_code, 124)
        self.assertIn("Command timed out", output)
        self.assertLessEqual(len(output), MAX_OUTPUT_LENGTH)

    def test_reader_shutdown_is_bounded_for_inherited_pipe(self) -> None:
        # A descendant retaining stdout can leave the reader blocked after the
        # parent process exits. The shutdown path must close the stream rather
        # than waiting forever for EOF.
        from app.test_runner import _finish_reader

        reader = Mock()
        reader.is_alive.side_effect = [True, False]
        stream = Mock()

        _finish_reader(reader, stream)

        reader.join.assert_any_call(timeout=5)
        stream.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
