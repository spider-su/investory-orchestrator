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


def prepare_workspace(
    client: GitHubAppClient,
    issue_number: int,
) -> tuple[Path, str]:
    root = Path(
        os.getenv(
            "WORKSPACES_DIR",
            "/home/alex/investory-orchestrator/workspaces",
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

    _run(
        [
            "git",
            "clone",
            f"https://github.com/{client.repository_name}.git",
            str(workspace),
        ],
        env=environment,
    )

    _mark_safe_directory(workspace)

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
) -> str | None:
    _mark_safe_directory(workspace)

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
    _run(["git", "commit", "-m", message], cwd=workspace)

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
) -> str | None:
    message = f"Complete {step_id}: {step_title}"

    if expected_parent_sha is None:
        commit_sha = commit_changes(workspace, message)
    else:
        commit_sha = commit_changes(
            workspace,
            message,
            expected_parent_sha=expected_parent_sha,
        )

    return commit_sha


def finalize_checkpoint_history(
    workspace: Path,
    *,
    baseline_sha: str,
    expected_checkpoint_sha: str,
    issue_number: int,
    issue_title: str,
) -> str:
    """Replace local checkpoint commits with one final logical commit."""
    _mark_safe_directory(workspace)

    _run(
        ["git", "cat-file", "-e", f"{baseline_sha}^{{commit}}"],
        cwd=workspace,
    )

    actual_checkpoint_sha = _run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
    ).strip()

    if actual_checkpoint_sha != expected_checkpoint_sha:
        raise RuntimeError(
            "Workspace HEAD changed before final history rewrite: "
            f"expected {expected_checkpoint_sha}, "
            f"got {actual_checkpoint_sha}"
        )

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
    expected_remote_sha: str | None = None,
) -> None:
    _mark_safe_directory(workspace)

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
