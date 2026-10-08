from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.agents.reviewer import review_identity, review_implementation
from app.tasks import TaskStore


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, separators=(",", ":"), default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _task_store() -> TaskStore:
    database = os.getenv("DATABASE_URL") or os.getenv("TASK_DB", "/app/data/tasks.db")
    return TaskStore(database)


def run_review_job(job_id: str, attempt_id: str) -> int:
    store = _task_store()
    job = store.get_job(job_id)
    if not job or job.get("kind") != "REVIEW":
        raise ValueError("runner job is not an independent review")
    if (
        job.get("status") != "running"
        or job.get("current_attempt_id") != attempt_id
    ):
        raise RuntimeError("review job attempt is stale")
    spec = job["job_spec"]
    request = spec.get("review_request")
    if not isinstance(request, dict) or request.get("task_id") != spec.get("task_id"):
        raise ValueError("review request task id does not match runner job")
    workspaces_dir = Path(
        os.getenv(
            "RUNNER_WORKSPACES_DIR",
            os.getenv("WORKSPACES_DIR", "~/.investory-orchestrator/task-workspaces"),
        )
    ).expanduser().resolve()
    workspace = Path(str(request.get("workspace", ""))).expanduser().resolve()
    if workspace == workspaces_dir or not workspace.is_relative_to(workspaces_dir):
        raise ValueError("review workspace is outside WORKSPACES_DIR")
    if not workspace.is_dir():
        raise ValueError("review workspace does not exist")
    expected_branch = request.get("expected_branch")
    expected_head_sha = request.get("expected_head_sha")
    if not isinstance(expected_branch, str) or not expected_branch:
        raise ValueError("invalid expected review branch")
    if not isinstance(expected_head_sha, str) or len(expected_head_sha) != 40:
        raise ValueError("invalid expected review commit")

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=workspace,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if head_sha != expected_head_sha or branch != expected_branch or dirty:
        raise RuntimeError("review workspace does not match the clean reviewed PR head")

    review = review_implementation(
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
    result = {
        "task_id": spec["task_id"],
        "head_sha": head_sha,
        "branch": branch,
        "clean_worktree": True,
        "review": review.model_dump(mode="json"),
        "reviewer_identity": review_identity(),
    }
    result_path = Path(os.environ["RUNNER_JOB_RESULT_PATH"])
    _atomic_json(result_path, result)
    print(json.dumps({
        "event": "final_review_finished",
        "job_id": job_id,
        "task_id": spec["task_id"],
        "head_sha": head_sha,
        "status": result["review"]["status"],
    }, separators=(",", ":")), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    try:
        return run_review_job(args.job_id, args.attempt_id)
    except Exception as error:
        result_path = os.getenv("RUNNER_JOB_RESULT_PATH", "")
        if result_path:
            _atomic_json(Path(result_path), {
                "task_id": "",
                "error": f"{type(error).__name__}: {error}",
                "failure_classification": type(error).__name__,
            })
        print(f"Final review job failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
