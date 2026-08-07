from __future__ import annotations

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
