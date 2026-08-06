from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class RetryIsolationError(RuntimeError):
    pass


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RetryIsolationError(
            f"Command failed: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    return result.stdout


def current_head(workspace: Path) -> str:
    return _run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
    ).strip()


def workspace_has_changes(workspace: Path) -> bool:
    return bool(
        _run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
        ).strip()
    )


def _safe_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return safe or "unknown"


def _artifact_directory(
    issue_number: int,
    step_id: str,
) -> Path:
    root = Path(os.getenv("RUNS_DIR", "/app/data/runs"))
    return (
        root
        / f"issue-{issue_number}"
        / _safe_component(step_id)
    )


def _unique_prefix(
    directory: Path,
    attempt: int,
    failure_stage: str,
) -> str:
    base = (
        f"attempt-{attempt}-"
        f"{_safe_component(failure_stage)}"
    )
    prefix = base
    sequence = 2

    while any(directory.glob(f"{prefix}.*")):
        prefix = f"{base}-{sequence}"
        sequence += 1

    return prefix


def _capture_patch(
    workspace: Path,
    baseline_sha: str,
) -> str:
    _run(
        ["git", "cat-file", "-e", f"{baseline_sha}^{{commit}}"],
        cwd=workspace,
    )
    _run(["git", "add", "--all"], cwd=workspace)

    try:
        return _run(
            [
                "git",
                "diff",
                "--cached",
                "--binary",
                baseline_sha,
                "--",
                ".",
            ],
            cwd=workspace,
        )
    finally:
        _run(
            ["git", "reset", "--mixed", baseline_sha],
            cwd=workspace,
        )


def reset_to_baseline(
    workspace: Path,
    baseline_sha: str,
) -> None:
    _run(
        ["git", "reset", "--hard", baseline_sha],
        cwd=workspace,
    )
    _run(["git", "clean", "-fd"], cwd=workspace)


def archive_and_reset_failed_attempt(
    *,
    workspace: Path,
    issue_number: int,
    step_id: str,
    attempt: int,
    failure_stage: str,
    baseline_sha: str,
    coder_summary: str,
    validation_output: str,
    validation_exit_code: int,
    review: dict[str, Any],
) -> dict[str, Any]:
    directory = _artifact_directory(issue_number, step_id)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = _unique_prefix(
        directory,
        attempt,
        failure_stage,
    )

    patch = _capture_patch(workspace, baseline_sha)

    patch_path = directory / f"{prefix}.patch"
    summary_path = directory / f"{prefix}-summary.txt"
    validation_path = directory / f"{prefix}-validation.txt"
    review_path = directory / f"{prefix}-review.json"
    metadata_path = directory / f"{prefix}-metadata.json"

    patch_path.write_text(patch, encoding="utf-8")
    summary_path.write_text(
        coder_summary or "No coder summary was recorded.\n",
        encoding="utf-8",
    )
    validation_path.write_text(
        validation_output or "No validation output was recorded.\n",
        encoding="utf-8",
    )
    review_path.write_text(
        json.dumps(review or {}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    artifact: dict[str, Any] = {
        "issue_number": issue_number,
        "step_id": step_id,
        "attempt": attempt,
        "failure_stage": failure_stage,
        "baseline_sha": baseline_sha,
        "patch_path": str(patch_path),
        "summary_path": str(summary_path),
        "validation_path": str(validation_path),
        "review_path": str(review_path),
        "metadata_path": str(metadata_path),
        "validation_exit_code": validation_exit_code,
    }

    metadata_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    reset_to_baseline(workspace, baseline_sha)
    return artifact
