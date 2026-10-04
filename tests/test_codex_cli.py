from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from app.agents.codex_cli import CodexCliError, codex_environment, run_structured_prompt


class ExampleResponse(BaseModel):
    title: str
    details: list[str]


class CodexCliTests(unittest.TestCase):
    def test_codex_environment_uses_local_auth_without_api_or_github_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "api-key",
                "GITHUB_APP_PRIVATE_KEY": "private-key",
                "GITHUB_TOKEN": "github-token",
                "GITHUB_APP_ID": "123",
                "PATH": os.environ.get("PATH", ""),
            },
            clear=True,
        ):
            environment = codex_environment()

        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GITHUB_APP_ID", environment)
        self.assertIn("PATH", environment)

    def test_runs_codex_with_read_only_sandbox_and_validates_schema_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            captured = {}

            def fake_run(command, **kwargs):
                captured.update(command=command, **kwargs)
                output_file = Path(command[command.index("--output-last-message") + 1])
                output_file.write_text(
                    json.dumps({"title": "Plan", "details": ["Inspect"]}),
                    encoding="utf-8",
                )
                schema_file = Path(command[command.index("--output-schema") + 1])
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
                captured["schema"] = schema
                return SimpleNamespace(returncode=0, stdout="Codex done")

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-pass"}),
                patch("app.agents.codex_cli.subprocess.run", side_effect=fake_run),
            ):
                result = run_structured_prompt(
                    role="planner",
                    prompt="Make a plan.",
                    response_model=ExampleResponse,
                    workspace=workspace,
                    model="local-plan-model",
                )

        self.assertEqual(result, ExampleResponse(title="Plan", details=["Inspect"]))
        self.assertEqual(captured["command"][0:4], ["codex", "exec", "--sandbox", "read-only"])
        self.assertIn("--model", captured["command"])
        self.assertEqual(captured["cwd"], workspace)
        self.assertEqual(captured["input"], "Make a plan.")
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertEqual(captured["schema"]["additionalProperties"], False)
        self.assertEqual(captured["schema"]["required"], ["title", "details"])

    def test_wraps_nonzero_codex_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.agents.codex_cli.subprocess.run",
                return_value=SimpleNamespace(returncode=2, stdout="login required"),
            ):
                with self.assertRaisesRegex(
                    CodexCliError,
                    "(?s)exit code 2.*login required",
                ):
                    run_structured_prompt(
                        role="reviewer",
                        prompt="Review.",
                        response_model=ExampleResponse,
                        workspace=Path(directory),
                    )


if __name__ == "__main__":
    unittest.main()
