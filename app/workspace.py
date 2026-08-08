from __future__ import annotations

import base64
import os
import subprocess
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

    _run(["git", "add", "--all"], cwd=workspace)

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
) -> str | None:
    message = f"Complete {step_id}: {step_title}"

    commit_kwargs: dict[str, str] = {}
    if expected_parent_sha is not None:
        commit_kwargs["expected_parent_sha"] = expected_parent_sha
    if operation_id is not None:
        commit_kwargs["operation_id"] = operation_id

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

    if actual_checkpoint_sha == expected_checkpoint_sha:
        # Preserve both committed checkpoint changes and any approved
        # whole-plan repair that is still uncommitted.
        _run(["git", "reset", "--soft", baseline_sha], cwd=workspace)

    _run(["git", "add", "--all"], cwd=workspace)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", "."],
        cwd=workspace,
        text=True,
        capture_output=True,
    )

    if staged.returncode == 0:
        raise RuntimeError(
            "No implementation changes remain after checkpoint "
            "history was reset."
        )

    if staged.returncode != 1:
        raise RuntimeError(
            "Could not inspect staged final implementation.\n"
            f"stdout:\n{staged.stdout}\n"
            f"stderr:\n{staged.stderr}"
        )

    _run(
        [
            "git",
            "commit",
            "-m",
            f"Implement #{issue_number}: {issue_title}",
            "--trailer",
            f"Investory-Operation-Id: {operation_id}",
        ],
        cwd=workspace,
    )

    return _run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
    ).strip()


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
