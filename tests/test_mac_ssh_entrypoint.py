from __future__ import annotations

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.mac_ssh_entrypoint import _lock_path, _probe, _run


class MacSshEntrypointTests(unittest.TestCase):
    def test_forced_command_runs_only_validated_task_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    os.environ,
                    {
                        "MAC_RUNNER_LOCK_DIR": directory,
                        "MAC_CLI_PYTHON": "/repo/.venv/bin/python",
                    },
                    clear=False,
                ),
                patch("scripts.mac_ssh_entrypoint.subprocess.call", return_value=0) as call,
            ):
                result = _run(["run", "spider-su/investory#20", "20", "develop", "1", "0"])
        self.assertEqual(result, 0)
        self.assertEqual(
            call.call_args.args[0],
            ["/repo/.venv/bin/python", "-m", "app", "--task-id", "spider-su/investory#20", "--issue", "20", "--resume"],
        )
        self.assertEqual(call.call_args.kwargs["env"]["BASE_BRANCH"], "develop")

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
