from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict


WorkflowStatus = Literal[
    "new",
    "planning",
    "implementing",
    "validating",
    "reviewing",
    "blocked",
    "waiting_ci",
    "completed",
]

ValidationStatus = Literal[
    "not_started",
    "environment_failure",
    "project_validation_failure",
    "validation_success",
]

ReviewStatus = Literal[
    "not_started",
    "approved",
    "changes_required",
    "review_failure",
]

CiStatus = Literal[
    "not_started",
    "queued",
    "in_progress",
    "success",
    "failure",
]


class WorkflowState(TypedDict):
    issue_number: int
    issue_title: str
    issue_body: str

    workflow_status: WorkflowStatus

    plan: dict[str, Any]
    plan_markdown: str
    plan_published: bool
    planning_error: str
    requires_user_input: bool

    steps: list[dict[str, Any]]
    current_step: int
    completed_steps: list[str]

    workspace: str
    branch: str

    attempt: int
    max_attempts: int

    coder_summary: str
    coder_error: str

    environment_ready: bool
    environment_output: str

    validation_status: ValidationStatus
    validation_exit_code: int
    test_output: str
    tests_passed: bool

    review_status: ReviewStatus
    review: dict[str, Any]
    review_markdown: str
    review_published: bool
    review_error: str

    commit_sha: Optional[str]

    pull_request_number: int
    pull_request_url: str

    ci_status: CiStatus
    ci_run_id: int
    ci_url: str
    ci_output: str

    blocked_reason: str
    error: str