from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class CodexCliError(RuntimeError):
    pass


def _strict_schema(value: object) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            properties = value.get("properties", {})
            value["additionalProperties"] = False
            value["required"] = list(properties)
        for child in value.values():
            _strict_schema(child)
    elif isinstance(value, list):
        for child in value:
            _strict_schema(child)


def codex_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_INSTALLATION_ID",
        "GITHUB_PRIVATE_KEY_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_TOKEN",
    ):
        environment.pop(name, None)
    return environment


def run_structured_prompt(
    *,
    role: str,
    prompt: str,
    response_model: type[ResponseModel],
    workspace: Path,
    model: str = "",
    timeout: int = 1800,
) -> ResponseModel:
    """Run a local Codex CLI role with strict structured output."""
    with tempfile.TemporaryDirectory(prefix=f"investory-{role}-") as directory:
        temporary = Path(directory)
        schema_path = temporary / f"{role}-schema.json"
        output_path = temporary / f"{role}-result.json"
        schema = response_model.model_json_schema(by_alias=True)
        _strict_schema(schema)
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        command = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if model:
            command.extend(["--model", model])
        command.append("-")

        try:
            execution = subprocess.run(
                command,
                cwd=workspace,
                env=codex_environment(),
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            raise CodexCliError(
                f"Codex {role} timed out after {timeout} seconds.\n{output}"
            ) from error
        except OSError as error:
            raise CodexCliError(
                f"Could not start Codex CLI for {role}: {error}"
            ) from error

        output = execution.stdout or "(Codex produced no output)"
        if execution.returncode != 0:
            raise CodexCliError(
                f"Codex {role} failed with exit code {execution.returncode}:\n"
                f"{output}"
            )
        if not output_path.is_file():
            raise CodexCliError(
                f"Codex {role} did not write its structured result.\n{output}"
            )
        try:
            return response_model.model_validate_json(
                output_path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise CodexCliError(
                f"Codex {role} returned invalid structured output: {error}"
            ) from error
