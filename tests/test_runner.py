from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.test_runner import (
    DevContainerScriptAdapter,
    ExecutionResult,
    MAX_OUTPUT_LENGTH,
    _run,
    run_validation,
    start_environment,
    stop_environment,
)


class TestRunnerTests(unittest.TestCase):
    workspace = Path("/tmp/issue-42")

    @patch("app.test_runner._configured_target_adapter")
    def test_start_environment_delegates_to_target_adapter(
        self,
        adapter_factory,
    ) -> None:
        adapter = Mock()
        adapter.start_environment.return_value = {
            "status": "success",
            "success": True,
            "exit_code": 0,
            "output": "ready",
        }
        adapter_factory.return_value = adapter

        result = start_environment(self.workspace, 42)

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "ready")
        adapter.start_environment.assert_called_once_with(self.workspace, 42)

    @patch("app.test_runner._configured_target_adapter")
    def test_validation_delegates_to_target_adapter(
        self,
        adapter_factory,
    ) -> None:
        adapter = Mock()
        adapter.run_validation.return_value = {
            "status": "project_validation_failure",
            "success": False,
            "exit_code": 2,
            "output": "validation failed",
        }
        adapter_factory.return_value = adapter

        result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "project_validation_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 2)
        adapter.run_validation.assert_called_once_with(self.workspace, 42)

    @patch("app.test_runner._configured_target_adapter")
    def test_stop_environment_delegates_to_target_adapter(
        self,
        adapter_factory,
    ) -> None:
        adapter = Mock()
        adapter.stop_environment.return_value = {
            "status": "environment_failure",
            "success": False,
            "exit_code": 1,
            "output": "container still running",
        }
        adapter_factory.return_value = adapter

        result = stop_environment(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertFalse(result["success"])
        adapter.stop_environment.assert_called_once_with(self.workspace, 42)

    def test_missing_devcontainer_script_is_environment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = DevContainerScriptAdapter().run_validation(
                Path(directory),
                42,
            )

        self.assertEqual(result["status"], "environment_failure")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("script was not found", result["output"])

    @patch("app.test_runner._run")
    def test_unusable_bash_is_environment_failure(self, run_mock) -> None:
        run_mock.return_value = ExecutionResult(1, "bash is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = DevContainerScriptAdapter().run_validation(workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Could not initialize bash", result["output"])
        run_mock.assert_called_once_with(
            ["bash", "-c", "exit 0"],
            workspace=workspace,
            timeout=30,
        )

    @patch("app.test_runner._run")
    def test_script_result_controls_validation_classification(
        self,
        run_mock,
    ) -> None:
        def fake_run(command, *, workspace, timeout):
            if command == ["bash", "-c", "exit 0"]:
                return ExecutionResult(0, "bash ready")

            result_path = Path(command[-1])
            result_path.write_text(
                json.dumps(
                    {
                        "status": "project_validation_failure",
                        "exit_code": 127,
                        "message": "Test command was not found.",
                    }
                ),
                encoding="utf-8",
            )
            return ExecutionResult(127, "test command failed")

        run_mock.side_effect = fake_run
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = DevContainerScriptAdapter().run_validation(workspace, 42)

        self.assertEqual(result["status"], "project_validation_failure")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("Test command was not found", result["output"])

    @patch("app.test_runner._run")
    def test_missing_or_malformed_result_is_environment_failure(
        self,
        run_mock,
    ) -> None:
        run_mock.side_effect = [
            ExecutionResult(0, "bash ready"),
            ExecutionResult(1, "Docker daemon unavailable"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = DevContainerScriptAdapter().run_validation(workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertIn("did not produce a valid result file", result["output"])

    @patch("app.test_runner._run")
    def test_malformed_result_is_environment_failure(self, run_mock) -> None:
        def fake_run(command, *, workspace, timeout):
            if command == ["bash", "-c", "exit 0"]:
                return ExecutionResult(0, "bash ready")

            Path(command[-1]).write_text("not json", encoding="utf-8")
            return ExecutionResult(1, "Docker daemon unavailable")

        run_mock.side_effect = fake_run
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = DevContainerScriptAdapter().run_validation(workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertIn("did not produce a valid result file", result["output"])

    @patch("app.test_runner._run")
    def test_execution_timeout_is_environment_failure(self, run_mock) -> None:
        run_mock.side_effect = [
            ExecutionResult(0, "bash ready"),
            ExecutionResult(124, "timed out", kind="timeout"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "scripts" / "agent-devcontainer.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            result = DevContainerScriptAdapter().run_validation(workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertEqual(result["exit_code"], 124)

    def test_unknown_target_adapter_is_environment_failure(self) -> None:
        with patch.dict(
            os.environ,
            {"TARGET_ADAPTER": "unknown"},
            clear=False,
        ):
            result = run_validation(self.workspace, 42)

        self.assertEqual(result["status"], "environment_failure")
        self.assertIn("Unsupported target adapter", result["output"])

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
