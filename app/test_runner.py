from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import BinaryIO
from typing import Literal, TypedDict


MAX_OUTPUT_LENGTH = 100_000
DEFAULT_DEVCONTAINER_SCRIPT = Path("scripts/agent-devcontainer.sh")
DEVCONTAINER_SCRIPT_ENVIRONMENT_VARIABLE = "AGENT_DEVCONTAINER_SCRIPT"


CommandStatus = Literal[
    "success",
    "environment_failure",
    "project_validation_failure",
]

RunFailure = Literal["none", "spawn", "timeout", "preflight"]


@dataclass(frozen=True)
class _RunResult:
    exit_code: int
    output: str
    failure: RunFailure = "none"

    def __iter__(self):
        # Preserve the existing private helper's two-value unpacking contract.
        yield self.exit_code
        yield self.output


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
            try:
                chunk = stream.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            output.feed(chunk)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _finish_reader(reader: Thread, stream: BinaryIO) -> None:
    """Avoid waiting indefinitely for a descendant holding stdout open."""
    reader.join(timeout=5)
    if reader.is_alive():
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        reader.join(timeout=1)


def _run(
    command: list[str],
    *,
    workspace: Path,
    timeout: int,
) -> _RunResult:
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
        return _RunResult(
            127,
            f"Could not start command: {error}",
            failure="spawn",
        )

    if process.stdout is None:
        process.terminate()
        process.wait()
        return _RunResult(
            127,
            "Could not capture command output",
            failure="spawn",
        )

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
        _finish_reader(reader, process.stdout)
        output_text = output.text()
        return _RunResult(
            124,
            _limit_output(
                f"{output_text}\nCommand timed out after {timeout} seconds."
            ),
            failure="timeout",
        )

    _finish_reader(reader, process.stdout)
    output_text = output.text()
    return _RunResult(
        process.returncode,
        output_text or "(command produced no output)",
    )


def _is_environment_failure(exit_code: int) -> bool:
    # 124 is the runner's timeout result; 127 is the conventional command or
    # interpreter-not-found result. Neither represents a project test result.
    return exit_code in {124, 127}


def _devcontainer_script(workspace: Path) -> Path:
    configured_path = Path(
        os.getenv(
            DEVCONTAINER_SCRIPT_ENVIRONMENT_VARIABLE,
            str(DEFAULT_DEVCONTAINER_SCRIPT),
        )
    )
    return (
        configured_path
        if configured_path.is_absolute()
        else workspace / configured_path
    )


def _run_devcontainer_action(
    action: Literal["up", "validate", "down"],
    *,
    workspace: Path,
    issue_number: int,
    timeout: int,
) -> _RunResult:
    script = _devcontainer_script(workspace)
    if not script.is_file():
        return _RunResult(
            127,
            (
                "Dev Container command script was not found: "
                f"{script}. Configure "
                f"{DEVCONTAINER_SCRIPT_ENVIRONMENT_VARIABLE} or add "
                "the required target-repository script."
            ),
            failure="preflight",
        )

    shell_check = _run(
        ["bash", "-c", "exit 0"],
        workspace=workspace,
        timeout=30,
    )
    if shell_check.exit_code != 0:
        return _RunResult(
            shell_check.exit_code,
            "Could not initialize bash for Dev Container commands:\n"
            f"{shell_check.output}",
            failure="preflight",
        )

    return _run(
        ["bash", str(script), action, str(issue_number)],
        workspace=workspace,
        timeout=timeout,
    )


def start_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    run_result = _run_devcontainer_action(
        "up",
        workspace=workspace,
        issue_number=issue_number,
        timeout=1800,
    )

    exit_code, output = run_result
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
    run_result = _run_devcontainer_action(
        "validate",
        workspace=workspace,
        issue_number=issue_number,
        timeout=1800,
    )

    exit_code, output = run_result
    failure = getattr(run_result, "failure", None)
    environment_failure = (
        failure in {"spawn", "timeout", "preflight"}
        if failure is not None
        else _is_environment_failure(exit_code)
    )

    return {
        "status": (
            "success"
            if exit_code == 0
            else (
                "environment_failure"
                if environment_failure
                else "project_validation_failure"
            )
        ),
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
    }


def stop_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    run_result = _run_devcontainer_action(
        "down",
        workspace=workspace,
        issue_number=issue_number,
        timeout=300,
    )

    exit_code, output = run_result
    return {
        "status": "success" if exit_code == 0 else "environment_failure",
        "success": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
    }
