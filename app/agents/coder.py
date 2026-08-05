from __future__ import annotations

import os
import subprocess
from pathlib import Path


class CoderError(RuntimeError):
    pass


def _git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        return f"Unable to read git diff:\n{result.stderr}"

    return result.stdout


def run_coder(
    *,
    workspace: Path,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    validation_output: str,
    attempt: int,
    max_attempts: int,
) -> str:
    prompt = f"""
Implement GitHub issue #{issue_number} in the current repository.

Title:
{issue_title}

Description:
{issue_body}

Attempt:
{attempt} of {max_attempts}

Previous validation output:
{validation_output or "No previous validation failure."}

Current git diff:
{_git_diff(workspace) or "No uncommitted changes."}

Instructions:
- Inspect AGENTS.md and repository documentation before editing.
- Implement only this issue.
- Make the smallest correct change.
- Add or update tests when required.
- Do not weaken, skip, or delete tests to obtain a passing build.
- Do not modify unrelated files.
- Do not access or print secrets.
- Leave all edits in the current workspace.
- Do not commit, push, or create a pull request.
- The orchestrator will run the complete validation separately.

When finished, return a concise summary of the implementation.
""".strip()

    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--full-auto",
    ]

    model = os.getenv("CODER_MODEL")
    if model:
        command.extend(["--model", model])

    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""

        if isinstance(output, bytes):
            output = output.decode(errors="replace")

        raise CoderError(
            f"Coder timed out after 1800 seconds.\n{output}"
        ) from error
    except OSError as error:
        raise CoderError(
            f"Could not start Codex CLI: {error}"
