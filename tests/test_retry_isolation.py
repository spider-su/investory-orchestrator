from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.retry_isolation import (
    archive_and_reset_failed_attempt,
    current_head,
    workspace_has_changes,
)


def _git(workspace: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


class RetryIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.workspace = root / "workspace"
        self.runs = root / "runs"
        self.workspace.mkdir()

        _git(self.workspace, "init")
        _git(self.workspace, "config", "user.name", "Test")
        _git(
            self.workspace,
            "config",
            "user.email",
            "test@example.com",
        )

        tracked = self.workspace / "tracked.txt"
        tracked.write_text("approved\n", encoding="utf-8")
        _git(self.workspace, "add", "tracked.txt")
        _git(self.workspace, "commit", "-m", "baseline")
        self.baseline = current_head(self.workspace)

    def test_archives_tracked_and_untracked_changes_then_resets(self) -> None:
        (self.workspace / "tracked.txt").write_text(
            "failed candidate\n",
            encoding="utf-8",
        )
        (self.workspace / "new.txt").write_text(
            "untracked candidate\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"RUNS_DIR": str(self.runs)},
        ):
            artifact = archive_and_reset_failed_attempt(
                workspace=self.workspace,
                issue_number=20,
                step_id="step-01",
                attempt=1,
                failure_stage="validation",
                baseline_sha=self.baseline,
                coder_summary="Implemented a candidate.",
                validation_output="Tests failed.",
                validation_exit_code=1,
                review={},
            )

        patch_text = Path(artifact["patch_path"]).read_text(
            encoding="utf-8"
        )

        self.assertIn("tracked.txt", patch_text)
        self.assertIn("failed candidate", patch_text)
        self.assertIn("new.txt", patch_text)
        self.assertIn("untracked candidate", patch_text)
        self.assertEqual(
            (self.workspace / "tracked.txt").read_text(
                encoding="utf-8"
            ),
            "approved\n",
        )
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertFalse(workspace_has_changes(self.workspace))
        self.assertEqual(current_head(self.workspace), self.baseline)
        self.assertTrue(Path(artifact["metadata_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
