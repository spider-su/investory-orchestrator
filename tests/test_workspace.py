from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

from app.workspace import (
    _validate_existing_workspace,
    commit_step,
)


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

    def test_existing_workspace_requires_matching_origin_and_clean_tree(self) -> None:
        workspace = Path("D:/projects/investory-orchestrator/workspaces/issue-1")
        client = SimpleNamespace(repository_name="owner/repository")

        with patch(
            "app.workspace._run",
            side_effect=[
                "agent/issue-1\n",
                "https://github.com/owner/repository.git\n",
                "",
            ],
        ):
            _validate_existing_workspace(
                workspace,
                client=client,
                branch="agent/issue-1",
            )

        with patch(
            "app.workspace._run",
            side_effect=[
                "agent/issue-1\n",
                "https://github.com/other/repository.git\n",
                "",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "origin"):
                _validate_existing_workspace(
                    workspace,
                    client=client,
                    branch="agent/issue-1",
                )

        with patch(
            "app.workspace._run",
            side_effect=[
                "agent/issue-1\n",
                "https://github.com/owner/repository.git\n",
                " M app.py\n",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "uncommitted"):
                _validate_existing_workspace(
                    workspace,
                    client=client,
                    branch="agent/issue-1",
                )


if __name__ == "__main__":
    unittest.main()
