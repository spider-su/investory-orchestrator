from __future__ import annotations

import fcntl
import hashlib
import json
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


def _review(arguments: list[str]) -> int:
    if len(arguments) != 2:
        raise ValueError("review expects one task id")
    task_id = _task_id(arguments[1])
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("review request must be a JSON object on stdin") from error
    if not isinstance(request, dict) or request.get("task_id") != task_id:
        raise ValueError("review request task id does not match the SSH command")

    workspaces_dir = Path(
        os.getenv("MAC_WORKSPACES_DIR", "~/.investory-orchestrator/task-workspaces")
    ).expanduser().resolve()
    workspace = Path(str(request.get("workspace", ""))).expanduser().resolve()
    if workspace == workspaces_dir or not workspace.is_relative_to(workspaces_dir):
        raise ValueError("review workspace is outside MAC_WORKSPACES_DIR")
    if not workspace.is_dir():
        raise ValueError("review workspace does not exist")
    expected_branch = request.get("expected_branch")
    expected_head_sha = request.get("expected_head_sha")
    if (
        not isinstance(expected_branch, str)
        or not BRANCH_PATTERN.fullmatch(expected_branch)
    ):
        raise ValueError("invalid expected review branch")
    if (
        not isinstance(expected_head_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha)
    ):
        raise ValueError("invalid expected review commit")

    lock_path = _lock_path(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("task has an active Mac worker") from error

        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=workspace, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if head_sha != expected_head_sha:
            raise RuntimeError("Mac workspace HEAD does not match the PR head SHA")
        if branch != expected_branch:
            raise RuntimeError("Mac workspace branch does not match the PR head branch")
        if status:
            raise RuntimeError(
                "Mac workspace is not clean; final review requires a clean worktree"
            )

        from app.agents.reviewer import review_implementation, review_identity

        result = review_implementation(
            workspace=workspace,
            issue_number=int(request["issue_number"]),
            issue_title=str(request["issue_title"]),
            issue_body=str(request.get("issue_body", "")),
            plan=request["plan"],
            validation_output=str(request.get("validation_output", "")),
            review_scope="whole_plan",
            baseline_sha=request.get("baseline_sha") or None,
            coder_report=request.get("coder_report"),
            workspace_audit=request.get("workspace_audit"),
        )
        response = {
            "task_id": task_id,
            "head_sha": head_sha,
            "branch": branch,
            "clean_worktree": True,
            "review": result.model_dump(mode="json"),
            "reviewer_identity": review_identity(),
        }
        print(json.dumps(response, separators=(",", ":")))
    return 0


def main() -> int:
    try:
        arguments = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        if len(arguments) == 2 and arguments[0] == "probe":
            return _probe(arguments[1])
        if arguments and arguments[0] == "run":
            return _run(arguments)
        if arguments and arguments[0] == "review":
            return _review(arguments)
        raise ValueError("only run, review, and probe requests are accepted")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Rejected Mac runner request: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
