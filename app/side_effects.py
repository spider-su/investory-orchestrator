from __future__ import annotations

import hashlib
from typing import Any, Mapping


def _operation_id(
    *,
    issue_number: int,
    kind: str,
    branch: str,
    target_sha: str,
) -> str:
    return (
        f"issue-{issue_number}:{kind}:"
        f"{branch}:{target_sha}"
    )


def prepare_push_intent(
    *,
    issue_number: int,
    branch: str,
    target_sha: str,
    expected_remote_sha: str | None,
) -> dict[str, Any]:
    return {
        "operation_id": _operation_id(
            issue_number=issue_number,
            kind="push-branch",
            branch=branch,
            target_sha=target_sha,
        ),
        "kind": "push_branch",
        "status": "prepared",
        "branch": branch,
        "target_sha": target_sha,
        "expected_remote_sha": expected_remote_sha or "",
    }


def prepare_draft_pr_intent(
    *,
    issue_number: int,
    branch: str,
    target_sha: str,
) -> dict[str, Any]:
    return {
        "operation_id": _operation_id(
            issue_number=issue_number,
            kind="draft-pr",
            branch=branch,
            target_sha=target_sha,
        ),
        "kind": "draft_pr_upsert",
        "status": "prepared",
        "branch": branch,
        "target_sha": target_sha,
    }


def prepare_issue_comment_intent(
    *,
    issue_number: int,
    comment_kind: str,
    marker: str,
    body: str,
    resume_node: str,
) -> dict[str, Any]:
    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    return {
        "operation_id": (
            f"issue-{issue_number}:comment:{comment_kind}:{body_digest}"
        ),
        "kind": "issue_comment",
        "status": "prepared",
        "issue_number": issue_number,
        "comment_kind": comment_kind,
        "marker": marker,
        "body_digest": body_digest,
        "resume_node": resume_node,
    }


def prepare_checkpoint_intent(
    *,
    issue_number: int,
    step_id: str,
    step_title: str,
    expected_parent_sha: str,
) -> dict[str, Any]:
    return {
        "operation_id": (
            f"issue-{issue_number}:checkpoint:{step_id}:"
            f"{expected_parent_sha}"
        ),
        "kind": "checkpoint",
        "status": "prepared",
        "step_id": step_id,
        "step_title": step_title,
        "expected_parent_sha": expected_parent_sha,
        "resume_node": "complete_step",
    }


def prepare_finalization_intent(
    *,
    issue_number: int,
    baseline_sha: str,
    checkpoint_sha: str,
) -> dict[str, Any]:
    return {
        "operation_id": (
            f"issue-{issue_number}:finalize-history:"
            f"{baseline_sha}:{checkpoint_sha}"
        ),
        "kind": "finalization",
        "status": "prepared",
        "baseline_sha": baseline_sha,
        "checkpoint_sha": checkpoint_sha,
        "resume_node": "finalize_history",
    }


def complete_intent(
    intent: Mapping[str, Any],
    history: list[dict[str, Any]],
    **evidence: Any,
) -> list[dict[str, Any]]:
    completed = {
        **dict(intent),
        "status": "completed",
        **evidence,
    }
    return [*history, completed]


def has_prepared_intent(state: Mapping[str, Any]) -> bool:
    intent = state.get("side_effect_intent", {})
    return (
        isinstance(intent, Mapping)
        and intent.get("status") == "prepared"
        and bool(intent.get("operation_id"))
    )
