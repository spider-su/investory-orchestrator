from __future__ import annotations

import os
import signal
import subprocess
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import BinaryIO, Literal, Protocol, TypedDict


MAX_OUTPUT_LENGTH = 100_000
MAX_RESULT_FILE_LENGTH = 100_000
DEFAULT_DEVCONTAINER_SCRIPT = Path("scripts/agent-devcontainer.sh")
DEVCONTAINER_SCRIPT_ENVIRONMENT_VARIABLE = "AGENT_DEVCONTAINER_SCRIPT"
TARGET_ADAPTER_ENVIRONMENT_VARIABLE = "TARGET_ADAPTER"
DEVCONTAINER_SCRIPT_ADAPTER = "devcontainer_script"


CommandStatus = Literal[
    "success",
    "environment_failure",
    "project_validation_failure",
]

ExecutionKind = Literal["completed", "spawn_failure", "timeout"]


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    output: str
    kind: ExecutionKind = "completed"

    def __iter__(self):
        # Preserve the existing private helper's two-value unpacking contract.
        yield self.exit_code
        yield self.output


class CommandResult(TypedDict):
    status: CommandStatus
    success: bool
    exit_code: int
    output: str


class TargetAdapter(Protocol):
    def start_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult: ...

    def run_validation(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult: ...

    def stop_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult: ...


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


def _combined_output(*parts: str) -> str:
    return _limit_output("\n".join(part for part in parts if part))


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
) -> ExecutionResult:
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
        return ExecutionResult(
            127,
            f"Could not start command: {error}",
            kind="spawn_failure",
        )

    if process.stdout is None:
        process.terminate()
        process.wait()
        return ExecutionResult(
            127,
            "Could not capture command output",
            kind="spawn_failure",
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
        return ExecutionResult(
            124,
            _limit_output(
                f"{output_text}\nCommand timed out after {timeout} seconds."
            ),
            kind="timeout",
        )

    _finish_reader(reader, process.stdout)
    output_text = output.text()
    return ExecutionResult(
        process.returncode,
        output_text or "(command produced no output)",
    )


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


def _result(
    status: CommandStatus,
    *,
    exit_code: int,
    output: str,
) -> CommandResult:
    return {
        "status": status,
        "success": status == "success",
        "exit_code": exit_code,
        "output": output,
    }


def _environment_failure(
    execution: ExecutionResult,
    message: str = "",
) -> CommandResult:
    output = _combined_output(message, execution.output)
    return _result(
        "environment_failure",
        exit_code=execution.exit_code,
        output=output,
    )


class DevContainerScriptAdapter:
    """Target adapter for the repository-owned Dev Container script protocol."""

    def start_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._run_action(
            "up",
            workspace=workspace,
            issue_number=issue_number,
            timeout=1800,
        )

    def run_validation(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._run_action(
            "validate",
            workspace=workspace,
            issue_number=issue_number,
            timeout=1800,
        )

    def stop_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._run_action(
            "down",
            workspace=workspace,
            issue_number=issue_number,
            timeout=300,
        )

    def _run_action(
        self,
        action: Literal["up", "validate", "down"],
        *,
        workspace: Path,
        issue_number: int,
        timeout: int,
    ) -> CommandResult:
        script = _devcontainer_script(workspace)
        if not script.is_file():
            return _result(
                "environment_failure",
                exit_code=127,
                output=(
                    "Dev Container command script was not found: "
                    f"{script}. Configure "
                    f"{DEVCONTAINER_SCRIPT_ENVIRONMENT_VARIABLE} or add "
                    "the required target-repository script."
                ),
            )

        shell_check = _run(
            ["bash", "-c", "exit 0"],
            workspace=workspace,
            timeout=30,
        )
        if shell_check.kind != "completed" or shell_check.exit_code != 0:
            return _environment_failure(
                shell_check,
                "Could not initialize bash for Dev Container commands:",
            )

        with tempfile.TemporaryDirectory(
            prefix="investory-target-result-"
        ) as directory:
            result_path = Path(directory) / "result.json"
            execution = _run(
                [
                    "bash",
                    str(script),
                    action,
                    str(issue_number),
                    "--result-file",
                    str(result_path),
                ],
                workspace=workspace,
                timeout=timeout,
            )
            return self._interpret_action_result(execution, result_path)

    @staticmethod
    def _interpret_action_result(
        execution: ExecutionResult,
        result_path: Path,
    ) -> CommandResult:
        if execution.kind != "completed":
            return _environment_failure(execution)

        try:
            if result_path.stat().st_size > MAX_RESULT_FILE_LENGTH:
                raise OSError(
                    "result file exceeds "
                    f"{MAX_RESULT_FILE_LENGTH} bytes"
                )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return _environment_failure(
                execution,
                "Dev Container command did not produce a valid result file: "
                f"{error}",
            )

        status = payload.get("status") if isinstance(payload, dict) else None
        reported_exit_code = (
            payload.get("exit_code") if isinstance(payload, dict) else None
        )
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        valid_statuses: set[str] = {
            "success",
            "project_validation_failure",
            "environment_failure",
        }
        if (
            status not in valid_statuses
            or not isinstance(reported_exit_code, int)
            or isinstance(reported_exit_code, bool)
            or not isinstance(message, str)
            or reported_exit_code != execution.exit_code
            or (status == "success") != (execution.exit_code == 0)
        ):
            return _environment_failure(
                execution,
                "Dev Container command produced an invalid result payload.",
            )

        output = _combined_output(message, execution.output)
        return _result(
            status,
            exit_code=execution.exit_code,
            output=output or "(command produced no output)",
        )


class _UnsupportedTargetAdapter:
    def __init__(self, configured_adapter: str) -> None:
        self.configured_adapter = configured_adapter

    def _failure(self) -> CommandResult:
        return _result(
            "environment_failure",
            exit_code=127,
            output=(
                f"Unsupported target adapter: {self.configured_adapter}. "
                f"Set {TARGET_ADAPTER_ENVIRONMENT_VARIABLE} to "
                f"{DEVCONTAINER_SCRIPT_ADAPTER}."
            ),
        )

    def start_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._failure()

    def run_validation(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._failure()

    def stop_environment(
        self,
        workspace: Path,
        issue_number: int,
    ) -> CommandResult:
        return self._failure()


def _configured_target_adapter() -> TargetAdapter:
    configured_adapter = os.getenv(
        TARGET_ADAPTER_ENVIRONMENT_VARIABLE,
        DEVCONTAINER_SCRIPT_ADAPTER,
    )
    if configured_adapter == DEVCONTAINER_SCRIPT_ADAPTER:
        return DevContainerScriptAdapter()

    return _UnsupportedTargetAdapter(configured_adapter)


def start_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    return _configured_target_adapter().start_environment(
        workspace,
        issue_number,
    )


def run_validation(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    return _configured_target_adapter().run_validation(
        workspace,
        issue_number,
    )


def stop_environment(
    workspace: Path,
    issue_number: int,
) -> CommandResult:
    return _configured_target_adapter().stop_environment(
        workspace,
        issue_number,
    )
