from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from app.github_client import GitHubAppClient


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    return result.stdout


def _git_environment(token: str) -> dict[str, str]:
    environment = os.environ.copy()

    credentials = f"x-access-token:{token}"
    encoded = base64.b64encode(
        credentials.encode("utf-8")
    ).decode("ascii")

    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = (
        "http.https://github.com/.extraheader"
    )
    environment["GIT_CONFIG_VALUE_0"] = (
        f"AUTHORIZATION: basic {encoded}"
    )

    return environment


def _mark_safe_directory(workspace: Path) -> None:
    resolved_workspace = str(workspace.resolve())
    existing = subprocess.run(
        [
            "git",
            "config",
            "--global",
            "--get-all",
            "safe.directory",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    if resolved_workspace in existing.stdout.splitlines():
        return

    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            resolved_workspace,
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def _configure_git_identity(workspace: Path) -> None:
    _run(
        ["git", "config", "user.name", "Investory Orchestrator"],
        cwd=workspace,
    )

    _run(
        [
            "git",
            "config",
            "user.email",
            "investory-orchestrator@users.noreply.github.com",
        ],
        cwd=workspace,
    )


def _repository_name(remote_url: str) -> str:
    normalized = remote_url.strip().removesuffix("/")

    if normalized.startswith("git@github.com:"):
        normalized = normalized.removeprefix("git@github.com:")
    elif "github.com/" in normalized:
        normalized = normalized.split("github.com/", 1)[1]

    return normalized.removesuffix(".git")


def _validate_existing_workspace(
    workspace: Path,
    *,
    client: GitHubAppClient,
    branch: str,
) -> None:
    current_branch = _run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
    ).strip()

    if current_branch != branch:
        raise RuntimeError(
            f"Existing workspace uses branch "
            f"'{current_branch}', expected '{branch}'"
        )

    remote_url = _run(
        ["git", "remote", "get-url", "origin"],
        cwd=workspace,
    ).strip()
    expected_repository = client.repository_name.removesuffix(".git")

    if _repository_name(remote_url) != expected_repository:
        raise RuntimeError(
            "Existing workspace origin does not match configured "
            f"repository '{client.repository_name}': {remote_url}"
        )

    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workspace,
    ).strip()

    if status:
        raise RuntimeError(
            "Existing workspace contains uncommitted changes; "
            "refusing to reuse it.\n"
            f"{status}"
        )

    remote_head_sha = client.get_branch_head_sha(branch)
    if remote_head_sha is not None:
        local_head_sha = _run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
        ).strip()

        if local_head_sha != remote_head_sha:
            raise RuntimeError(
                "Existing workspace HEAD does not match remote branch "
                f"'{branch}': local {local_head_sha}, "
                f"remote {remote_head_sha}"
            )


def _find_commit_by_operation_id(
    workspace: Path,
    operation_id: str,
) -> tuple[str, str] | None:
    output = _run(
        [
            "git",
            "log",
            "--all",
            "--format=%H%x00%P%x00%B%x00",
        ],
        cwd=workspace,
    )
    fields = output.split("\x00")

    for index in range(0, len(fields) - 2, 3):
        commit_sha, parent_sha, body = fields[index:index + 3]
        if f"Investory-Operation-Id: {operation_id}" in body:
            return commit_sha, parent_sha

    return None


def _changed_paths(workspace: Path) -> list[str]:
    output = _run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        cwd=workspace,
    )
    paths: list[str] = []

    for line in output.splitlines():
        if len(line) < 4:
            continue

        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]

        paths.append(Path(path).as_posix())

    return paths


def _path_is_allowed(path: str, allowed_paths: Iterable[str]) -> bool:
    raw_path = Path(path).as_posix()
    normalized_path = raw_path.lstrip("./")
    name = Path(raw_path).name.lower()

    if (
        name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or name in {"id_rsa", "id_ed25519"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    ):
        return False

    for allowed in allowed_paths:
        raw_allowed = Path(str(allowed)).as_posix()
        if raw_allowed in {".", ""}:
            return True

        normalized_allowed = raw_allowed.lstrip("./").rstrip("/")

        if (
            normalized_path == normalized_allowed
            or normalized_path.startswith(f"{normalized_allowed}/")
        ):
            return True

    return False


def _stage_changed_paths(
    workspace: Path,
    *,
    allowed_paths: Iterable[str] | None = None,
) -> list[str]:
    paths = _changed_paths(workspace)

    if allowed_paths is not None:
        disallowed = [
            path
            for path in paths
            if not _path_is_allowed(path, allowed_paths)
        ]
        if disallowed:
            raise RuntimeError(
                "Changes outside the approved path scope: "
                + ", ".join(disallowed)
            )

    if paths:
        _run(
            ["git", "add", "--", *paths],
            cwd=workspace,
        )

    return paths


def prepare_workspace(
    client: GitHubAppClient,
    issue_number: int,
) -> tuple[Path, str]:
    root = Path(
        os.getenv(
            "WORKSPACES_DIR",
            "/app/workspaces",
        )
    )

    workspace = root / f"issue-{issue_number}"
    branch = f"agent/issue-{issue_number}"

    if workspace.exists():
        _mark_safe_directory(workspace)

        _validate_existing_workspace(
            workspace,
            client=client,
            branch=branch,
        )

        _configure_git_identity(workspace)
        return workspace, branch

    workspace.parent.mkdir(parents=True, exist_ok=True)

    environment = _git_environment(client.token)
    remote_branch_sha = client.get_branch_head_sha(branch)

    clone_command = [
        "git",
        "clone",
    ]

    if remote_branch_sha:
        clone_command.extend(["--branch", branch])

    clone_command.extend(
        [
            f"https://github.com/{client.repository_name}.git",
            str(workspace),
        ]
    )

    _run(
        clone_command,
        env=environment,
    )

    _mark_safe_directory(workspace)

    if not remote_branch_sha:
        _run(
            ["git", "checkout", "-b", branch],
            cwd=workspace,
        )

    _configure_git_identity(workspace)

    return workspace, branch


def commit_changes(
    workspace: Path,
    message: str,
    *,
    expected_parent_sha: str | None = None,
    operation_id: str | None = None,
    allowed_paths: Iterable[str] | None = None,
) -> str | None:
    _mark_safe_directory(workspace)

    if operation_id is not None:
        existing_commit = _find_commit_by_operation_id(
            workspace,
            operation_id,
        )

        if existing_commit is not None:
            actual_head_sha = _run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
            ).strip()

            if actual_head_sha == existing_commit[0]:
                return actual_head_sha

            raise RuntimeError(
                "Checkpoint operation already exists away from HEAD: "
                f"{existing_commit[0]}"
            )

    if expected_parent_sha is not None:
        actual_parent_sha = _run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
        ).strip()

        if actual_parent_sha != expected_parent_sha:
            raise RuntimeError(
                "Workspace HEAD changed before checkpoint commit: "
                f"expected {expected_parent_sha}, got {actual_parent_sha}"
            )

    status = _run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
    ).strip()

    if not status:
        return None

    _stage_changed_paths(
        workspace,
        allowed_paths=allowed_paths,
    )

    commit_command = ["git", "commit", "-m", message]
    if operation_id is not None:
        commit_command.extend(
            [
                "--trailer",
                f"Investory-Operation-Id: {operation_id}",
            ]
        )

    _run(commit_command, cwd=workspace)

    return _run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
    ).strip()


def commit_step(
    workspace: Path,
    step_id: str,
    step_title: str,
    *,
    expected_parent_sha: str | None = None,
    operation_id: str | None = None,
    allowed_paths: Iterable[str] | None = None,
) -> str | None:
    message = f"Complete {step_id}: {step_title}"

    commit_kwargs: dict[str, str] = {}
    if expected_parent_sha is not None:
        commit_kwargs["expected_parent_sha"] = expected_parent_sha
    if operation_id is not None:
        commit_kwargs["operation_id"] = operation_id
    if allowed_paths is not None:
        commit_kwargs["allowed_paths"] = allowed_paths

    commit_sha = commit_changes(
        workspace,
        message,
        **commit_kwargs,
    )

    return commit_sha


def finalize_checkpoint_history(
    workspace: Path,
    *,
    baseline_sha: str,
    expected_checkpoint_sha: str,
    operation_id: str,
    issue_number: int,
    issue_title: str,
    allowed_paths: Iterable[str] | None = None,
) -> str:
    """Replace local checkpoint commits with one final logical commit."""
    _mark_safe_directory(workspace)

    _run(
        ["git", "cat-file", "-e", f"{baseline_sha}^{{commit}}"],
        cwd=workspace,
    )

    existing_commit = _find_commit_by_operation_id(
        workspace,
        operation_id,
    )
    if existing_commit is not None:
        actual_head_sha = _run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
        ).strip()

        if actual_head_sha == existing_commit[0]:
            return actual_head_sha

        raise RuntimeError(
            "Finalization operation already exists away from HEAD: "
            f"{existing_commit[0]}"
        )

    actual_checkpoint_sha = _run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
    ).strip()

    if actual_checkpoint_sha != expected_checkpoint_sha:
        if actual_checkpoint_sha != baseline_sha:
            raise RuntimeError(
                "Workspace HEAD changed before final history rewrite: "
                f"expected {expected_checkpoint_sha} or {baseline_sha}, "
                f"got {actual_checkpoint_sha}"
            )

    changed_paths = _changed_paths(workspace)
    if allowed_paths is not None:
        disallowed = [
            path
            for path in changed_paths
            if not _path_is_allowed(path, allowed_paths)
        ]
        if disallowed:
            raise RuntimeError(
                "Changes outside the approved path scope: "
                + ", ".join(disallowed)
            )

    # Clear only the index. Keep worktree edits intact, then stage approved
    # paths and create the final commit object without moving HEAD first.
    _run(["git", "reset", "--mixed", "HEAD"], cwd=workspace)
    if changed_paths:
        _run(["git", "add", "--", *changed_paths], cwd=workspace)

    tree_sha = _run(["git", "write-tree"], cwd=workspace).strip()
    baseline_tree_sha = _run(
        ["git", "rev-parse", f"{baseline_sha}^{{tree}}"],
        cwd=workspace,
    ).strip()

    if tree_sha == baseline_tree_sha:
        raise RuntimeError("No implementation changes remain for final commit.")

    final_sha = _run(
        [
            "git",
            "commit-tree",
            tree_sha,
            "-p",
            baseline_sha,
            "-m",
            f"Implement #{issue_number}: {issue_title}",
            "-m",
            f"Investory-Operation-Id: {operation_id}",
        ],
        cwd=workspace,
    ).strip()

    head_ref = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    ref_name = head_ref.stdout.strip() if head_ref.returncode == 0 else "HEAD"

    _run(
        [
            "git",
            "update-ref",
            ref_name,
            final_sha,
            actual_checkpoint_sha,
        ],
        cwd=workspace,
    )

    return final_sha


def push_branch(
    client: GitHubAppClient,
    workspace: Path,
    branch: str,
    *,
    expected_local_sha: str | None = None,
    expected_remote_sha: str | None = None,
) -> None:
    _mark_safe_directory(workspace)

    if expected_local_sha is not None:
        actual_local_sha = _run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
        ).strip()

        if actual_local_sha != expected_local_sha:
            raise RuntimeError(
                "Workspace HEAD does not match intended push commit: "
                f"expected {expected_local_sha}, got {actual_local_sha}"
            )

    environment = _git_environment(client.token)
    protected_ref = f"refs/heads/{branch}"
    lease = expected_remote_sha or ""

    _run(
        [
            "git",
            "push",
            "-u",
            f"--force-with-lease={protected_ref}:{lease}",
            "origin",
            f"HEAD:{protected_ref}",
        ],
        cwd=workspace,
        env=environment,
    )
