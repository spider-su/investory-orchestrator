from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Literal, TypedDict


MAX_OUTPUT_LENGTH = 100_000


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


def _text_output(output: str | bytes | None) -> str:
    if output is None:
        return ""

    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")

    return output


def _limit_output(output: str) -> str:
    if len(output) <= MAX_OUTPUT_LENGTH:
        return output

    marker = "\n... <output truncated> ...\n"
    available = MAX_OUTPUT_LENGTH - len(marker)
    head_length = available // 2
    tail_length = available - head_length
    return (
        output[:head_length]
        + marker
        + output[-tail_length:]
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        process.wait()


def _run(
    command: list[str],
    *,
    workspace: Path,
    timeout: int,
) -> tuple[int, str]:
    process_options: dict[str, object] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True

    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **process_options,
        )
    except OSError as error:
        return 127, f"Could not start command: {error}"

    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        output, _ = process.communicate()
        output_text = _limit_output(_text_output(output))
        return 124, (
            f"{output_text}\nCommand timed out after {timeout} seconds."
        )

    output_text = _limit_output(_text_output(output))
    return process.returncode, output_text or "(command produced no output)"


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
