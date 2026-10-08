from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# SSH invokes this file by absolute path from the user's home directory. Make
# the checkout's package imports independent of the remote shell's cwd.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.runner_jobs import RunnerJobStore


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


def _job_store() -> RunnerJobStore:
    runs_dir = Path(
        os.getenv("MAC_RUNS_DIR", "~/.investory-orchestrator/runs")
    ).expanduser()
    return RunnerJobStore(runs_dir / "jobs")


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
    task_id = _task_id(task_id)
    store = _job_store()
    for path in store.directory.glob("[0-9a-f][0-9a-f]*.json"):
        job_id = path.stem
        if len(job_id) != 24:
            continue
        status = store.get(job_id, include_result=False)
        if status.get("task_id") == task_id and status.get("status") in {
            "submitted", "running",
        }:
            return 0
    # Compatibility check for workers started by the previous SSH protocol.
    # New workers are represented by durable job records above.
    lock_path = _lock_path(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        else:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    return 1


def _submit(arguments: list[str]) -> int:
    if len(arguments) != 2:
        raise ValueError("submit expects one job id")
    job_id = arguments[1]
    RunnerJobStore._validate_id(job_id)
    try:
        spec = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("submit request must be a JSON object on stdin") from error
    if not isinstance(spec, dict) or spec.get("job_id") != job_id:
        raise ValueError("submit request job id does not match the SSH command")
    task_id = _task_id(str(spec.get("task_id", "")))
    job_type = spec.get("job_type")
    if job_type not in {"workflow", "final_review"}:
        raise ValueError("unsupported runner job type")
    repository = str(spec.get("repository", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository identity")
    branch = str(spec.get("branch", ""))
    if branch and not BRANCH_PATTERN.fullmatch(branch):
        raise ValueError("invalid branch")

    _ensure_node_on_path(os.environ)
    root = Path(__file__).resolve().parents[1]
    runs_dir = Path(
        os.getenv("MAC_RUNS_DIR", "~/.investory-orchestrator/runs")
    ).expanduser().resolve()
    environment = os.environ.copy()
    environment["RUNS_DIR"] = str(runs_dir)
    environment["ORCHESTRATOR_RUNNER_RESULT_PATH"] = str(runs_dir / f"{job_id}.result.json")
    environment["ORCHESTRATOR_BUILD_SHA"] = str(spec.get("expected_build_sha", "unknown"))
    workspaces_dir = Path(
        os.getenv("MAC_WORKSPACES_DIR", "~/.investory-orchestrator/task-workspaces")
    ).expanduser().resolve()
    environment["WORKSPACES_DIR"] = str(workspaces_dir)
    if spec.get("base_branch"):
        environment["BASE_BRANCH"] = str(spec["base_branch"])

    python = os.getenv("MAC_CLI_PYTHON", "python3")
    if job_type == "workflow":
        if not isinstance(spec.get("task"), dict):
            raise ValueError("workflow job requires a task snapshot")
        command = [
            python, "-m", "app", "--task-id", task_id,
            "--runner-worker", "--runner-job-id", job_id,
        ]
        issue_number = spec["task"].get("issue_number")
        if issue_number is not None:
            if not isinstance(issue_number, int) or isinstance(issue_number, bool):
                raise ValueError("invalid issue number")
            command.extend(["--issue", str(issue_number)])
        if spec.get("resume") is True:
            command.append("--resume")
        if spec.get("ci_repair") is True:
            command.append("--ci-repair")
    else:
        request = spec.get("review_request")
        if not isinstance(request, dict) or request.get("task_id") != task_id:
            raise ValueError("final review job requires a matching review request")
        workspace = Path(str(request.get("workspace", ""))).expanduser().resolve()
        if workspace == workspaces_dir or not workspace.is_relative_to(workspaces_dir):
            raise ValueError("review workspace is outside MAC_WORKSPACES_DIR")
        command = [python, "-m", "app.review_worker", "--job-id", job_id]

    store = RunnerJobStore(runs_dir / "jobs")
    result = store.submit(
        job_id=job_id,
        spec=spec,
        command=command,
        environment={
            name: environment[name]
            for name in (
                "PATH", "HOME", "RUNS_DIR", "WORKSPACES_DIR", "BASE_BRANCH",
                "ORCHESTRATOR_RUNNER_RESULT_PATH", "ORCHESTRATOR_BUILD_SHA",
            )
            if name in environment
        },
        cwd=root,
    )
    print(json.dumps(result, separators=(",", ":"), default=str))
    return 0


def _job_status(arguments: list[str]) -> int:
    if len(arguments) != 2:
        raise ValueError("status expects one job id")
    print(json.dumps(
        _job_store().get(arguments[1]), separators=(",", ":"), default=str
    ))
    return 0


def _cancel(arguments: list[str]) -> int:
    if len(arguments) != 2:
        raise ValueError("cancel expects one job id")
    print(json.dumps(
        _job_store().cancel(arguments[1]), separators=(",", ":"), default=str
    ))
    return 0


def _codex_quota_report(environment: dict[str, str]) -> dict[str, object]:
    """Read the authenticated Codex account rate-limit snapshot without a model call."""
    process = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=environment,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Codex app-server pipes are unavailable")

        def send(message: dict[str, object]) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def response_for(request_id: int, timeout: float) -> dict[str, object]:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                readable, _, _ = select.select(
                    [process.stdout], [], [], max(0, deadline - time.monotonic())
                )
                if not readable:
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError("Codex account rate-limit request failed")
                    result = message.get("result")
                    if isinstance(result, dict):
                        return result
                    break
            raise TimeoutError("Codex account rate-limit request timed out")

        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "investory-orchestrator-quota-probe",
                    "title": "Investory Orchestrator quota probe",
                    "version": "1.0",
                },
                "capabilities": {},
            },
        })
        response_for(1, 5)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {},
        })
        result = response_for(2, 12)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise RuntimeError("Codex returned no account rate-limit snapshot")
    windows: list[dict[str, object]] = []
    for name in ("primary", "secondary"):
        window = rate_limits.get(name)
        if not isinstance(window, dict) or not isinstance(window.get("usedPercent"), (int, float)):
            continue
        remaining = max(0, min(100, 100 - int(window["usedPercent"])))
        windows.append({
            "name": name,
            "remaining_percent": remaining,
            "resets_at": window.get("resetsAt"),
        })
    if not windows:
        raise RuntimeError("Codex returned no usable account quota windows")
    return {
        "status": "available",
        "remaining_percent": min(int(window["remaining_percent"]) for window in windows),
        "windows": windows,
        "observed_at": time.time(),
    }


def _health_report(expected_sha: str) -> dict:
    if expected_sha != "unknown" and not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("invalid expected orchestrator revision")
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    checks: dict[str, object] = {}
    try:
        _ensure_node_on_path(environment)
        git = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        actual_sha = git.stdout.strip() if git.returncode == 0 else ""
        checks["git"] = git.returncode == 0
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        checks["code_clean"] = bool(actual_sha) and status.returncode == 0 and status.stdout.strip() == ""
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        actual_sha = ""
        checks["git"] = False
        checks["code_clean"] = False
    checks["revision_matches"] = expected_sha == "unknown" or actual_sha == expected_sha

    versions: dict[str, str] = {}
    for name, command in (
        ("git", ["git", "--version"]),
        ("gh", ["gh", "--version"]),
        ("codex", ["codex", "--version"]),
    ):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10,
                check=False, env=environment,
            )
            checks[f"{name}_installed"] = result.returncode == 0
            if result.returncode == 0 and result.stdout.strip():
                versions[name] = result.stdout.splitlines()[0][:120]
        except (OSError, subprocess.TimeoutExpired):
            checks[f"{name}_installed"] = False

    for name, command in (
        ("gh_authenticated", ["gh", "auth", "status", "--hostname", "github.com"]),
        ("codex_authenticated", ["codex", "login", "status"]),
    ):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10,
                check=False, env=environment,
            )
            checks[name] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            checks[name] = False

    try:
        quota = _codex_quota_report(environment)
    except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
        quota = {
            "status": "unavailable",
            "detail": "Codex quota could not be read; dispatch must pause until it is available.",
            "observed_at": time.time(),
        }

    workspaces = Path(
        os.getenv("MAC_WORKSPACES_DIR", "~/.investory-orchestrator/task-workspaces")
    ).expanduser()
    try:
        workspaces.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=workspaces, prefix="health-", delete=True):
            pass
        checks["workspace_writable"] = True
    except OSError:
        checks["workspace_writable"] = False

    ready = all(checks.values())
    failed = [name for name, passed in checks.items() if not passed]
    detail = (
        f"Mac runner ready at {actual_sha[:12]} ({versions.get('codex', 'Codex version unavailable')})."
        if ready
        else "Mac runner unavailable; failed checks: " + ", ".join(failed)
    )
    return {
        "status": "ready" if ready else "unavailable",
        "detail": detail,
        "checks": checks,
        "versions": versions,
        "codex_quota": quota,
        "actual_sha": actual_sha,
        "expected_sha": expected_sha,
    }


def _health(expected_sha: str) -> int:
    try:
        report = _health_report(expected_sha)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        report = {
            "status": "unavailable",
            "detail": f"Mac runner health probe failed: {type(error).__name__}.",
            "checks": {"health_probe": False},
            "versions": {},
            "expected_sha": expected_sha,
        }
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["status"] == "ready" else 1


def _run(arguments: list[str]) -> int:
    if len(arguments) != 7:
        raise ValueError("run expects task id, issue, branch, resume, ci-repair, and expected revision")
    task_id = _task_id(arguments[1])
    issue_argument, base_branch, resume, ci_repair, expected_sha = arguments[2:]
    if issue_argument != "-" and not re.fullmatch(r"-?[0-9]+", issue_argument):
        raise ValueError("invalid issue number")
    if base_branch != "-" and not BRANCH_PATTERN.fullmatch(base_branch):
        raise ValueError("invalid base branch")
    if resume not in {"0", "1"} or ci_repair not in {"0", "1"}:
        raise ValueError("invalid worker flags")
    health = _health_report(expected_sha)
    if health["status"] != "ready":
        print(json.dumps({"task_id": task_id, "runner": health}, separators=(",", ":")))
        return 78

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
        started_at = time.time()
        print(json.dumps({
            "timestamp": started_at, "task_id": task_id,
            "node": "mac_runner", "event": "worker_started",
            "expected_sha": expected_sha,
        }, separators=(",", ":")), flush=True)
        exit_code = subprocess.call(
            command, cwd=Path(__file__).resolve().parents[1], env=environment
        )
        print(json.dumps({
            "timestamp": time.time(), "task_id": task_id,
            "node": "mac_runner", "event": "worker_finished",
            "exit_code": exit_code, "duration_seconds": round(time.time() - started_at, 3),
        }, separators=(",", ":")), flush=True)
        return exit_code


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
        if arguments and arguments[0] == "submit":
            return _submit(arguments)
        if arguments and arguments[0] == "status":
            return _job_status(arguments)
        if arguments and arguments[0] == "cancel":
            return _cancel(arguments)
        if len(arguments) == 2 and arguments[0] == "health":
            return _health(arguments[1])
        if arguments and arguments[0] == "run":
            return _run(arguments)
        if arguments and arguments[0] == "review":
            return _review(arguments)
        raise ValueError("only health, submit, status, cancel, run, review, and probe requests are accepted")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Rejected Mac runner request: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
