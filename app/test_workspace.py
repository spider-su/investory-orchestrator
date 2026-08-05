from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.workspace import commit_step


class WorkspaceTests(unittest.TestCase):
    def test_commit_step_returns_new_commit_sha(self) -> None:
        workspace = Path("D:/projects/investory-orchestrator/workspaces/issue-1")

        with patch(
            "app.workspace.commit_changes",
            return_value="abc123",
        ) as commit_changes_mock:
            commit_sha = commit_step(
                workspace,
                "step-01",
                "Add tests",
            )

        commit_changes_mock.assert_called_once_with(
            workspace,
            "Complete step-01: Add tests",
        )
        self.assertEqual(commit_sha, "abc123")

    def test_commit_step_returns_none_for_noop_step(self) -> None:
        workspace = Path("D:/projects/investory-orchestrator/workspaces/issue-1")

        with patch(
            "app.workspace.commit_changes",
            return_value=None,
        ):
            commit_sha = commit_step(
                workspace,
                "step-01",
                "Add tests",
            )

        self.assertIsNone(commit_sha)


if __name__ == "__main__":
    unittest.main()
