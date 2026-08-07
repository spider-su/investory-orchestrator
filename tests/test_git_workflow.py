from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.github_client import GitHubAppClient


def run(
    command: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
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


def git_environment(token: str) -> dict[str, str]:
    env = os.environ.copy()

    credentials = f"x-access-token:{token}"
    encoded = base64.b64encode(
        credentials.encode("utf-8")
    ).decode("ascii")

    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = (
        "http.https://github.com/.extraheader"
    )
    env["GIT_CONFIG_VALUE_0"] = (
        f"AUTHORIZATION: basic {encoded}"
    )

    return env


def main() -> None:
    client = GitHubAppClient()
    repo = client.get_repository()

    timestamp = int(time.time())
    branch_name = f"agent/test-workflow-{timestamp}"

    print(f"Creating branch: {branch_name}")
    client.create_branch(branch_name)

    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory) / "investory"
        env = git_environment(client.token)

        print("Cloning repository")

        run(
            [
                "git",
                "clone",
                "--branch",
                branch_name,
                f"https://github.com/{client.repository_name}.git",
                str(workspace),
            ],
            env=env,
        )

        run(
            ["git", "config", "user.name", "Investory Orchestrator"],
            cwd=workspace,
        )
        run(
            [
                "git",
                "config",
                "user.email",
                "investory-orchestrator@users.noreply.github.com",
            ],
            cwd=workspace,
        )

        test_file = workspace / "orchestrator-write-test.txt"
        test_file.write_text(
            "Temporary GitHub App workflow test.\n",
            encoding="utf-8",
        )

        run(["git", "add", "orchestrator-write-test.txt"], cwd=workspace)

        run(
            ["git", "commit", "-m", "Test orchestrator Git access"],
            cwd=workspace,
        )

        print("Pushing branch")

        run(
            ["git", "push", "origin", branch_name],
            cwd=workspace,
            env=env,
        )

    print("Creating draft pull request")

    pull_request = client.create_draft_pr(
        title="Test orchestrator Git workflow",
        body=(
            "Temporary draft PR created to verify branch creation, "
            "clone, commit, push, and pull request permissions."
        ),
        head=branch_name,
    )

    print("Workflow test: OK")
    print(f"Branch: {branch_name}")
    print(f"Pull request: #{pull_request.number}")
    print(f"URL: {pull_request.html_url}")


if __name__ == "__main__":
    main()
