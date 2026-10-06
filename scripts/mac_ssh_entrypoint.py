from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import shlex
import subprocess
import sys
from pathlib import Path


TASK_ID_PATTERN = re.compile(
    r"^(?:[0-9]+|[A-Fa-f0-9]{12}|[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*#[0-9]+)$"
)
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def _lock_path(task_id: str) -> Path:
    key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    directory = Path(
        os.getenv("MAC_RUNNER_LOCK_DIR", "~/.investory-orchestrator/locks")
    ).expanduser()
    return directory / f"{key}.lock"


def _task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid task id")
    return value


def _ensure_node_on_path(environment: dict[str, str]) -> None:
    if shutil.which("node", path=environment.get("PATH")):
        return

    nvm_script = Path.home() / ".nvm" / "nvm.sh"
    if not nvm_script.is_file():
        return

    result = subprocess.run(
        ["/bin/bash", "-c", '. "$HOME/.nvm/nvm.sh" && nvm which default'],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    node_path = result.stdout.strip()
    if result.returncode != 0 or not node_path:
        raise RuntimeError(
            "Could not resolve the default Node.js binary from NVM: "
            f"{result.stderr.strip()}"
        )

    environment["PATH"] = os.pathsep.join(
        [str(Path(node_path).parent), environment.get("PATH", "")]
    )


def _probe(task_id: str) -> int:
    lock_path = _lock_path(_task_id(task_id))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
    return 1


def _run(arguments: list[str]) -> int:
    if len(arguments) != 6:
        raise ValueError("run expects task id, issue, branch, resume, and ci-repair")
    task_id = _task_id(arguments[1])
    issue_argument, base_branch, resume, ci_repair = arguments[2:]
    if issue_argument != "-" and not re.fullmatch(r"-?[0-9]+", issue_argument):
        raise ValueError("invalid issue number")
    if base_branch != "-" and not BRANCH_PATTERN.fullmatch(base_branch):
        raise ValueError("invalid base branch")
    if resume not in {"0", "1"} or ci_repair not in {"0", "1"}:
        raise ValueError("invalid worker flags")

    lock_path = _lock_path(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Task already has an active Mac worker", file=sys.stderr)
            return 73
        command = [os.getenv("MAC_CLI_PYTHON", "python3"), "-m", "app", "--task-id", task_id]
        if issue_argument != "-":
            command.extend(["--issue", issue_argument])
        if resume == "1":
            command.append("--resume")
        if ci_repair == "1":
            command.append("--ci-repair")
        environment = os.environ.copy()
        if base_branch != "-":
            environment["BASE_BRANCH"] = base_branch
        _ensure_node_on_path(environment)
        workspaces_dir = Path(
            os.getenv(
                "MAC_WORKSPACES_DIR",
                "~/.investory-orchestrator/task-workspaces",
            )
        ).expanduser()
        environment["WORKSPACES_DIR"] = str(workspaces_dir)
        runs_dir = Path(
            os.getenv(
                "MAC_RUNS_DIR",
                "~/.investory-orchestrator/runs",
            )
        ).expanduser()
        environment["RUNS_DIR"] = str(runs_dir)
        return subprocess.call(command, cwd=Path(__file__).resolve().parents[1], env=environment)


def main() -> int:
    try:
        arguments = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        if len(arguments) == 2 and arguments[0] == "probe":
            return _probe(arguments[1])
        if arguments and arguments[0] == "run":
            return _run(arguments)
        raise ValueError("only run and probe requests are accepted")
    except (OSError, ValueError) as error:
        print(f"Rejected Mac runner request: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
