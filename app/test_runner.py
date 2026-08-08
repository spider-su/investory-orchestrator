from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal, TypedDict


CommandStatus = Literal[
    "success",
    "environment_failure",
    "project_validation_failure",
]


class CommandResult(TypedDict):
    status: CommandStatus
    success: bool
    exit_code: int
    output: str


def _run(
    command: list[str],
    *,
    workspace: Path,
    timeout: int,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""

        if isinstance(output, bytes):
            output = output.decode(errors="replace")

        return 124, f"{output}\nCommand timed out after {timeout} seconds."
    except OSError as error:
        return 127, f"Could not start command: {error}"

    return result.returncode, result.stdout or "(command produced no output)"


def start_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    exit_code, output = _run(
        [
            "bash",
            "scripts/agent-devcontainer.sh",
            "up",
            str(issue_number),
        ],
        workspace=workspace,
        timeout=1800,
    )

    return {
        "status": "success" if exit_code == 0 else "environment_failure",
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
    }


def run_validation(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    exit_code, output = _run(
        [
            "bash",
            "scripts/agent-devcontainer.sh",
            "validate",
            str(issue_number),
        ],
        workspace=workspace,
        timeout=1800,
    )

    return {
        "status": (
            "success"
            if exit_code == 0
            else "project_validation_failure"
        ),
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
    }


def stop_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    exit_code, output = _run(
        [
            "bash",
            "scripts/agent-devcontainer.sh",
            "down",
            str(issue_number),
        ],
        workspace=workspace,
        timeout=300,
    )

    return {
        "status": "success" if exit_code == 0 else "environment_failure",
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
    }
