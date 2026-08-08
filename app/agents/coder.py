from __future__ import annotations

import os
import subprocess
from pathlib import Path


class CoderError(RuntimeError):
    pass


def _coder_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "GITHUB_APP_ID",
        "GITHUB_INSTALLATION_ID",
        "GITHUB_PRIVATE_KEY_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_TOKEN",
    ):
        environment.pop(name, None)

    return environment


def coder_identity() -> dict[str, str]:
    return {
        "backend": "codex-cli",
        "provider": os.getenv("CODER_PROVIDER", "unknown"),
        "model": os.getenv("CODER_MODEL", ""),
    }


def _git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        return (
            "Unable to read git diff.\n"
            f"stderr:\n{result.stderr}"
        )

    return result.stdout


def _read_failed_patch(path: str, *, limit: int = 20_000) -> str:
    if not path:
        return "No previous failed attempt patch."

    patch_path = Path(path)

    try:
        patch = patch_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return f"Unable to read failed patch at {path}: {error}"

    if len(patch) <= limit:
        return patch or "Previous failed attempt produced an empty patch."

    return patch[:limit] + "\n... <failed patch truncated>"


def run_coder(
    *,
    workspace: Path,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    step: dict,
    validation_output: str,
    review_feedback: dict,
    attempt: int,
    max_attempts: int,
    failed_patch_path: str,
) -> str:
    prompt = f"""
Implement GitHub issue #{issue_number} in the current repository.

Title:
{issue_title}

Description:
{issue_body or "No issue body was provided."}

Current implementation step:
{step}

Attempt:
{attempt} of {max_attempts}

Previous validation output:
{validation_output or "No previous validation failure."}

Reviewer feedback:
{review_feedback or "None"}

Current git diff:
{_git_diff(workspace) or "No uncommitted changes."}

Previous failed attempt patch (diagnostic context only):
{_read_failed_patch(failed_patch_path)}

Do not reapply the failed patch blindly. Produce a fresh candidate from the clean step baseline.

Instructions:
- Inspect AGENTS.md and repository documentation before editing.
- Implement only this issue.
- Make the smallest correct change.
- Add or update tests when required.
- Do not weaken, skip, or delete tests.
- Do not modify unrelated files.
- Do not access or print secrets.
- Leave all edits in the current workspace.
- Do not commit, push, or create a pull request.
- The orchestrator runs the complete validation suite separately.

Return a concise implementation summary.
""".strip()

    command = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "-",
    ]

    model = os.getenv("CODER_MODEL")
    if model:
        command.extend(["--model", model])

    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=_coder_environment(),
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""

        if isinstance(output, bytes):
            output = output.decode(
                "utf-8",
                errors="replace",
            )

        raise CoderError(
            "Coder timed out after 1800 seconds.\n"
            f"{output}"
        ) from error
    except OSError as error:
        raise CoderError(
            f"Could not start Codex CLI: {error}"
        ) from error

    output = result.stdout or "(Codex produced no output)"

    if result.returncode != 0:
        raise CoderError(
            f"Codex failed with exit code "
            f"{result.returncode}:\n"
            f"{output}"
        )

    return output
