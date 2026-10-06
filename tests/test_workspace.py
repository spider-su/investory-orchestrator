from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import tempfile
from types import SimpleNamespace

from app.workspace import (
    _validate_existing_workspace,
    commit_step,
    prepare_workspace,
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
        client = SimpleNamespace(
            repository_name="owner/repository",
            get_branch_head_sha=Mock(return_value="remote-sha"),
        )

        with patch(
            "app.workspace._run",
            side_effect=[
                "agent/issue-1\n",
                "https://github.com/owner/repository.git\n",
                "",
                "remote-sha\n",
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

    def test_prepare_workspace_clones_existing_remote_branch(self) -> None:
        client = SimpleNamespace(
            token="token",
            repository_name="owner/repository",
            get_branch_head_sha=Mock(return_value="remote-sha"),
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"WORKSPACES_DIR": directory}):
                with patch("app.workspace._run", return_value="") as run_mock:
                    with patch("app.workspace._mark_safe_directory"):
                        with patch("app.workspace._configure_git_identity"):
                            workspace, branch = prepare_workspace(client, 42)

        self.assertEqual(workspace, Path(directory) / "issue-42")
        self.assertEqual(branch, "agent/issue-42")
        run_mock.assert_any_call(
            [
                "git",
                "clone",
                "--bare",
                "https://github.com/owner/repository.git",
                str(Path(directory) / ".repositories" / "owner_repository.git"),
            ],
            env=ANY,
        )
        run_mock.assert_any_call(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(Path(directory) / ".repositories" / "owner_repository.git"),
                str(Path(directory) / "issue-42"),
            ],
            env=ANY,
        )
        run_mock.assert_any_call(
            [
                "git",
                "fetch",
                str(Path(directory) / ".repositories" / "owner_repository.git"),
                "refs/remotes/origin/agent/issue-42:refs/remotes/origin/agent/issue-42",
            ],
            cwd=Path(directory) / "issue-42",
        )
        run_mock.assert_any_call(
            [
                "git",
                "checkout",
                "-b",
                "agent/issue-42",
                "refs/remotes/origin/agent/issue-42",
            ],
            cwd=Path(directory) / "issue-42",
        )
        run_mock.assert_any_call(
            ["git", "remote", "set-url", "origin", "https://github.com/owner/repository.git"],
            cwd=Path(directory) / "issue-42",
        )

    def test_prompt_task_uses_task_workspace_and_branch(self) -> None:
        client = SimpleNamespace(
            token="token",
            repository_name="owner/repository",
            get_branch_head_sha=Mock(return_value=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"WORKSPACES_DIR": directory, "BASE_BRANCH": "develop"},
            ):
                with patch("app.workspace._run") as run_mock:
                    with patch("app.workspace._mark_safe_directory"):
                        with patch("app.workspace._configure_git_identity"):
                            workspace, branch = prepare_workspace(
                                client,
                                -123,
                                task_id="abc123",
                            )

        self.assertEqual(workspace, Path(directory) / "task-abc123")
        self.assertEqual(branch, "agent/task-abc123")
        run_mock.assert_any_call(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(Path(directory) / ".repositories" / "owner_repository.git"),
                str(Path(directory) / "task-abc123"),
            ],
            env=ANY,
        )
        run_mock.assert_any_call(
            [
                "git",
                "fetch",
                str(Path(directory) / ".repositories" / "owner_repository.git"),
                "refs/remotes/origin/develop:refs/remotes/origin/develop",
            ],
            cwd=Path(directory) / "task-abc123",
        )
        run_mock.assert_any_call(
            [
                "git",
                "checkout",
                "-b",
                "agent/task-abc123",
                "refs/remotes/origin/develop",
            ],
            cwd=Path(directory) / "task-abc123",
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

    def test_existing_workspace_rejects_stale_remote_head(self) -> None:
        workspace = Path("D:/projects/investory-orchestrator/workspaces/issue-1")
        client = SimpleNamespace(
            repository_name="owner/repository",
            get_branch_head_sha=Mock(return_value="remote-sha"),
        )

        with patch(
            "app.workspace._run",
            side_effect=[
                "agent/issue-1\n",
                "https://github.com/owner/repository.git\n",
                "",
                "local-sha\n",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                _validate_existing_workspace(
                    workspace,
                    client=client,
                    branch="agent/issue-1",
                )


if __name__ == "__main__":
    unittest.main()
