from __future__ import annotations

import base64
import os
import shutil
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
        current_branch = _run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
        ).strip()

        if current_branch != branch:
            raise RuntimeError(
                f"Existing workspace uses branch "
                f"'{current_branch}', expected '{branch}'"
            )

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

    _run(["git", "checkout", "-b", branch], cwd=workspace)

    return workspace, branch

    def push_branch(
        client: GitHubAppClient,
        workspace: Path,
        branch: str,
    ) -> None:
        environment = _git_environment(client.token)

        _run(
            ["git", "push", "-u", "origin", branch],
            cwd=workspace,
            env=environment,
        )
