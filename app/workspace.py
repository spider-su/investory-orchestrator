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
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            str(workspace),
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

        current_branch = _run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
        ).strip()

        if current_branch != branch:
            raise RuntimeError(
                f"Existing workspace uses branch "
                f"'{current_branch}', expected '{branch}'"
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
) -> str | None:
    _mark_safe_directory(workspace)

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
) -> str | None:
    commit_sha = commit_changes(
        workspace,
        f"Checkpoint {step_id}: {step_title}",
    )

    return commit_sha


def finalize_checkpoint_history(
    workspace: Path,
    *,
    baseline_sha: str,
    issue_number: int,
    issue_title: str,
) -> str:
    """Replace local checkpoint commits with one final logical commit."""
    _mark_safe_directory(workspace)

    _run(
        ["git", "cat-file", "-e", f"{baseline_sha}^{{commit}}"],
        cwd=workspace,
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