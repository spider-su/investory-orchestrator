from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.mac_ssh_entrypoint import (
    _ensure_node_on_path,
    _lock_path,
    _probe,
    _run,
)


class MacSshEntrypointTests(unittest.TestCase):
    def test_script_imports_repository_package_from_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["SSH_ORIGINAL_COMMAND"] = "invalid"
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/mac_ssh_entrypoint.py")],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 64)
        self.assertIn("only health, submit", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_forced_command_runs_only_validated_task_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    os.environ,
                    {
                        "MAC_RUNNER_LOCK_DIR": directory,
                        "MAC_CLI_PYTHON": "/repo/.venv/bin/python",
                        "MAC_WORKSPACES_DIR": f"{directory}/mac-workspaces",
                        "MAC_RUNS_DIR": f"{directory}/mac-runs",
                        "WORKSPACES_DIR": "/app/workspaces",
                    },
                    clear=False,
                ),
                patch("scripts.mac_ssh_entrypoint.subprocess.call", return_value=0) as call,
                patch("scripts.mac_ssh_entrypoint._health_report", return_value={"status": "ready"}),
            ):
                result = _run(["run", "spider-su/investory#20", "20", "develop", "1", "0", "unknown"])
        self.assertEqual(result, 0)
        self.assertEqual(
            call.call_args.args[0],
            ["/repo/.venv/bin/python", "-m", "app", "--task-id", "spider-su/investory#20", "--issue", "20", "--resume"],
        )
        self.assertEqual(call.call_args.kwargs["env"]["BASE_BRANCH"], "develop")
        self.assertEqual(
            call.call_args.kwargs["env"]["WORKSPACES_DIR"],
            f"{directory}/mac-workspaces",
        )
        self.assertEqual(
            call.call_args.kwargs["env"]["RUNS_DIR"],
            f"{directory}/mac-runs",
        )

    def test_workspace_directory_defaults_to_writable_mac_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    os.environ,
                    {
                        "MAC_RUNNER_LOCK_DIR": directory,
                        "HOME": directory,
                        "WORKSPACES_DIR": "/app/workspaces",
                    },
                    clear=False,
                ),
                patch.dict(os.environ, {"MAC_WORKSPACES_DIR": ""}, clear=False),
                patch("scripts.mac_ssh_entrypoint.subprocess.call", return_value=0) as call,
                patch("scripts.mac_ssh_entrypoint._health_report", return_value={"status": "ready"}),
            ):
                os.environ.pop("MAC_WORKSPACES_DIR", None)
                self.assertEqual(
                    _run(["run", "abcdef123456", "20", "develop", "0", "0", "unknown"]),
                    0,
                )

        self.assertEqual(
            call.call_args.kwargs["env"]["WORKSPACES_DIR"],
            f"{directory}/.investory-orchestrator/task-workspaces",
        )
        self.assertEqual(
            call.call_args.kwargs["env"]["RUNS_DIR"],
            f"{directory}/.investory-orchestrator/runs",
        )

    def test_resolves_default_nvm_node_when_not_on_ssh_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            nvm_script = home / ".nvm" / "nvm.sh"
            nvm_script.parent.mkdir(parents=True)
            nvm_script.touch()
            environment = {"HOME": directory, "PATH": "/usr/bin"}
            with (
                patch("scripts.mac_ssh_entrypoint.Path.home", return_value=home),
                patch("scripts.mac_ssh_entrypoint.shutil.which", return_value=None),
                patch(
                    "scripts.mac_ssh_entrypoint.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=f"{directory}/.nvm/versions/node/v24.0.0/bin/node\n",
                        stderr="",
                    ),
                ) as run_mock,
            ):
                _ensure_node_on_path(environment)

        self.assertEqual(
            environment["PATH"],
            f"{directory}/.nvm/versions/node/v24.0.0/bin:/usr/bin",
        )
        run_mock.assert_called_once()

    def test_probe_reports_active_lock_and_rejects_invalid_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"MAC_RUNNER_LOCK_DIR": directory}, clear=False):
                lock_path = _lock_path("abcdef123456")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(_probe("abcdef123456"), 0)
                self.assertEqual(_probe("abcdef123456"), 1)
                with self.assertRaisesRegex(ValueError, "invalid task id"):
                    _probe("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
