from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.workspace import (
    commit_step,
    finalize_checkpoint_history,
)


def _run(workspace: Path, *command: str) -> str:
    result = subprocess.run(
        list(command),
        cwd=workspace,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class WorkspaceFinalizationTest(unittest.TestCase):
    def test_commit_step_rejects_unapproved_and_sensitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _run(workspace, "git", "init")
            _run(workspace, "git", "config", "user.name", "Test User")
            _run(
                workspace,
                "git",
                "config",
                "user.email",
                "test@example.com",
            )

            source = workspace / "service.txt"
            source.write_text("baseline\n", encoding="utf-8")
            _run(workspace, "git", "add", "service.txt")
            _run(workspace, "git", "commit", "-m", "Baseline")

            source.write_text("changed\n", encoding="utf-8")
            unrelated = workspace / "unrelated.txt"
            unrelated.write_text("must not commit\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "outside"):
                commit_step(
                    workspace,
                    "step-01",
                    "Scoped change",
                    allowed_paths=["service.txt"],
                )

            unrelated.unlink()
            secret = workspace / ".env"
            secret.write_text("TOKEN=secret\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "outside"):
                commit_step(
                    workspace,
                    "step-01",
                    "Scoped change",
                    allowed_paths=["."],
                )

    def test_checkpoint_commits_are_replaced_by_one_final_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _run(workspace, "git", "init")
            _run(
                workspace,
                "git",
                "config",
                "user.name",
                "Test User",
            )
            _run(
                workspace,
                "git",
                "config",
                "user.email",
                "test@example.com",
            )

            source = workspace / "service.txt"
            source.write_text("baseline\n", encoding="utf-8")
            _run(workspace, "git", "add", "service.txt")
            _run(workspace, "git", "commit", "-m", "Baseline")
            baseline_sha = _run(
                workspace,
                "git",
                "rev-parse",
                "HEAD",
            )

            source.write_text(
                "baseline\nstep one\n",
                encoding="utf-8",
            )
            first_checkpoint = commit_step(
                workspace,
                "step-01",
                "Introduce abstraction",
                operation_id="issue-20:checkpoint:step-01:baseline",
            )
            self.assertIsNotNone(first_checkpoint)

            repeated_checkpoint = commit_step(
                workspace,
                "step-01",
                "Introduce abstraction",
                operation_id="issue-20:checkpoint:step-01:baseline",
            )
            self.assertEqual(first_checkpoint, repeated_checkpoint)

            source.write_text(
                "baseline\nstep one revised\nstep two\n",
                encoding="utf-8",
            )
            second_checkpoint = commit_step(
                workspace,
                "step-02",
                "Integrate abstraction",
            )
            self.assertIsNotNone(second_checkpoint)

            # Approved whole-plan repair remains uncommitted and must
            # be included in the final logical commit.
            source.write_text(
                "baseline\ncoherent final design\n",
                encoding="utf-8",
            )

            final_sha = finalize_checkpoint_history(
                workspace,
                baseline_sha=baseline_sha,
                expected_checkpoint_sha=second_checkpoint,
                operation_id=(
                    f"issue-20:finalize-history:{baseline_sha}:"
                    f"{second_checkpoint}"
                ),
                issue_number=20,
                issue_title="Test check",
            )

            self.assertEqual(
                "Implement #20: Test check",
                _run(
                    workspace,
                    "git",
                    "log",
                    "-1",
                    "--format=%s",
                ),
            )
            self.assertEqual(
                "2",
                _run(
                    workspace,
                    "git",
                    "rev-list",
                    "--count",
                    "HEAD",
                ),
            )
            self.assertEqual(
                "baseline\ncoherent final design\n",
                source.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                final_sha,
                _run(workspace, "git", "rev-parse", "HEAD"),
            )
            self.assertNotEqual(first_checkpoint, final_sha)
            self.assertNotEqual(second_checkpoint, final_sha)
            self.assertEqual(
                "",
                _run(workspace, "git", "status", "--porcelain"),
            )

            repeated_sha = finalize_checkpoint_history(
                workspace,
                baseline_sha=baseline_sha,
                expected_checkpoint_sha=second_checkpoint,
                operation_id=(
                    f"issue-20:finalize-history:{baseline_sha}:"
                    f"{second_checkpoint}"
                ),
                issue_number=20,
                issue_title="Test check",
            )
            self.assertEqual(final_sha, repeated_sha)

    def test_finalization_includes_clean_checkpoint_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _run(workspace, "git", "init")
            _run(workspace, "git", "config", "user.name", "Test User")
            _run(
                workspace,
                "git",
                "config",
                "user.email",
                "test@example.com",
            )

            source = workspace / "service.txt"
            source.write_text("baseline\n", encoding="utf-8")
            _run(workspace, "git", "add", "service.txt")
            _run(workspace, "git", "commit", "-m", "Baseline")
            baseline_sha = _run(workspace, "git", "rev-parse", "HEAD")

            source.write_text("baseline\nimplemented\n", encoding="utf-8")
            checkpoint_sha = commit_step(
                workspace,
                "step-01",
                "Implement change",
                operation_id="issue-21:checkpoint:step-01:baseline",
                allowed_paths=["service.txt"],
            )
            self.assertIsNotNone(checkpoint_sha)

            final_sha = finalize_checkpoint_history(
                workspace,
                baseline_sha=baseline_sha,
                expected_checkpoint_sha=checkpoint_sha,
                operation_id=(
                    f"issue-21:finalize-history:{baseline_sha}:"
                    f"{checkpoint_sha}"
                ),
                issue_number=21,
                issue_title="Clean checkpoint",
                allowed_paths=["service.txt"],
            )

            self.assertEqual(
                final_sha,
                _run(workspace, "git", "rev-parse", "HEAD"),
            )
            self.assertEqual(
                "baseline\nimplemented\n",
                source.read_text(encoding="utf-8"),
            )
