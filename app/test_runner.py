from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from threading import Thread
from typing import BinaryIO
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


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.marker = b"\n... <output truncated> ...\n"
        available = max(limit - len(self.marker), 0)
        self.head_limit = available // 2
        self.tail_limit = available - self.head_limit
        self.buffer = bytearray()
        self.head = bytearray()
        self.tail = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return

        if not self.truncated:
            combined = self.buffer + chunk
            if len(combined) <= self.limit:
                self.buffer = combined
                return

            self.head = combined[:self.head_limit]
            self.tail = combined[-self.tail_limit:] if self.tail_limit else bytearray()
            self.truncated = True
            return

        if self.tail_limit:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def text(self) -> str:
        if not self.truncated:
            output = bytes(self.buffer)
        else:
            output = bytes(self.head) + self.marker + bytes(self.tail)

        return output.decode("utf-8", errors="replace")


def _read_output(stream: BinaryIO, output: _BoundedOutput) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            output.feed(chunk)
    finally:
        stream.close()


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
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **process_options,
        )
    except OSError as error:
        return 127, f"Could not start command: {error}"

    if process.stdout is None:
        process.terminate()
        process.wait()
        return 127, "Could not capture command output"

    output = _BoundedOutput(MAX_OUTPUT_LENGTH)
    reader = Thread(
        target=_read_output,
        args=(process.stdout, output),
        daemon=True,
    )
    reader.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        reader.join(timeout=5)
        output_text = output.text()
        return 124, (
            f"{output_text}\nCommand timed out after {timeout} seconds."
        )

    reader.join()
    output_text = output.text()
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
