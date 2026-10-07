from __future__ import annotations

import subprocess
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.coder import CoderError, _git_diff, run_coder


class CompletedProcessResult:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path("D:/projects/investory-orchestrator")

    def test_git_diff_returns_stdout(self) -> None:
        with patch(
            "app.agents.coder.subprocess.run",
            return_value=CompletedProcessResult(
                returncode=0,
                stdout="diff --git a/app.py b/app.py",
            ),
        ):
            diff = _git_diff(self.workspace)

        self.assertEqual(diff, "diff --git a/app.py b/app.py")

    def test_git_diff_returns_error_summary_when_git_fails(self) -> None:
        with patch(
            "app.agents.coder.subprocess.run",
            return_value=CompletedProcessResult(
                returncode=1,
                stderr="fatal: not a git repository",
            ),
        ):
            diff = _git_diff(self.workspace)

        self.assertIn("Unable to read git diff.", diff)
        self.assertIn("fatal: not a git repository", diff)

    def test_run_coder_invokes_codex_and_returns_summary(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> CompletedProcessResult:
            captured.update(kwargs)
            captured["command"] = args[0]
            command = args[0]
            output_path = Path(
                command[command.index("--output-last-message") + 1]
            )
            output_path.write_text(
                json.dumps({
                    "status": "completed",
                    "summary": "Implemented tests for agents.",
                    "changes": ["tests/test_agents.py"],
                    "evidence": ["Inspected the planner tests."],
                    "testsRun": [],
                    "remainingProblems": [],
                    "needsHumanInput": False,
                }),
                encoding="utf-8",
            )
            return CompletedProcessResult(
                returncode=0,
                stdout="Implemented tests for agents.",
            )

        with patch("app.agents.coder._git_diff", return_value="git diff text"):
            with patch("app.agents.coder.os.getenv") as getenv_mock:
                getenv_mock.side_effect = lambda name: "gpt-coder" if name == "CODER_MODEL" else None

                with patch("app.agents.coder.subprocess.run", side_effect=fake_run):
                    summary = run_coder(
                        workspace=self.workspace,
                        issue_number=42,
                        issue_title="Add tests for agents",
                        issue_body="Cover planner, coder, and reviewer.",
                        step={"id": "step-01", "title": "Add tests"},
                        completed_step_results=[
                            {
                                "id": "step-01",
                                "title": "Gather evidence",
                                "coder": {
                                    "summary": "No safe cleanup candidates found.",
                                    "evidence": ["Reviewed runtime wiring."],
                                },
                            }
                        ],
                        validation_output="Previous validation failed.",
                        review_feedback={"status": "changes_required"},
                        attempt=2,
                        max_attempts=3,
                        failed_patch_path="",
                        workspace_audit={
                            "branch": "agent/issue-42",
                            "clean": True,
                        },
                    )

        self.assertEqual(summary.summary, "Implemented tests for agents.")
        command = captured["command"]
        self.assertEqual(command[:4], ["codex", "exec", "--sandbox", "workspace-write"])
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        self.assertEqual(command[-3:], ["--model", "gpt-coder", "-"])
        self.assertEqual(captured["cwd"], self.workspace)
        self.assertEqual(captured["timeout"], 1800)
        self.assertEqual(captured["stderr"], subprocess.STDOUT)
        self.assertNotIn("GITHUB_PRIVATE_KEY_PATH", captured["env"])
        self.assertNotIn("GITHUB_APP_ID", captured["env"])

        prompt = captured["input"]
        self.assertIn("Implement GitHub issue #42", prompt)
        self.assertIn('"testsRun"', prompt)
        self.assertIn('"needsHumanInput"', prompt)
        self.assertIn('"evidence"', prompt)
        self.assertIn("{'id': 'step-01', 'title': 'Add tests'}", prompt)
        self.assertIn("Previous validation failed.", prompt)
        self.assertIn("{'status': 'changes_required'}", prompt)
        self.assertIn("git diff text", prompt)
        self.assertIn("Implement exactly the current implementation step", prompt)
        self.assertIn("No safe cleanup candidates found.", prompt)
        self.assertIn("Reviewed runtime wiring.", prompt)
        self.assertIn("no-op can satisfy the step", prompt)
        self.assertIn('"branch": "agent/issue-42"', prompt)
        self.assertIn("Do not commit, push, or create a pull request.", prompt)

    def test_run_coder_wraps_timeout_output(self) -> None:
        with patch("app.agents.coder._git_diff", return_value=""):
            with patch(
                "app.agents.coder.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["codex"],
                    timeout=1800,
                    output=b"partial output",
                ),
            ):
                with self.assertRaises(CoderError) as context:
                    run_coder(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Timeout",
                        issue_body="",
                        step={"id": "step-01"},
                        validation_output="",
                        review_feedback={},
                        attempt=1,
                        max_attempts=3,
                        failed_patch_path="",
                    )

        self.assertIn("Coder timed out after 1800 seconds.", str(context.exception))
        self.assertIn("partial output", str(context.exception))

    def test_run_coder_wraps_os_errors(self) -> None:
        with patch("app.agents.coder._git_diff", return_value=""):
            with patch(
                "app.agents.coder.subprocess.run",
                side_effect=OSError("missing codex"),
            ):
                with self.assertRaises(CoderError) as context:
                    run_coder(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Missing CLI",
                        issue_body="",
                        step={"id": "step-01"},
                        validation_output="",
                        review_feedback={},
                        attempt=1,
                        max_attempts=3,
                        failed_patch_path="",
                    )

        self.assertIn("Could not start Codex CLI: missing codex", str(context.exception))

    def test_run_coder_raises_for_non_zero_exit_code(self) -> None:
        with patch("app.agents.coder._git_diff", return_value=""):
            with patch(
                "app.agents.coder.subprocess.run",
                return_value=CompletedProcessResult(
                    returncode=2,
                    stdout="codex failure output",
                ),
            ):
                with self.assertRaises(CoderError) as context:
                    run_coder(
                        workspace=self.workspace,
                        issue_number=1,
                        issue_title="Failed coder run",
                        issue_body="",
                        step={"id": "step-01"},
                        validation_output="",
                        review_feedback={},
                        attempt=1,
                        max_attempts=3,
                        failed_patch_path="",
                    )

        self.assertIn("Codex failed with exit code 2", str(context.exception))
        self.assertIn("codex failure output", str(context.exception))


if __name__ == "__main__":
    unittest.main()
