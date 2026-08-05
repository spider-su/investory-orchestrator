from typing import Any, Literal, TypedDict


ReviewStatus = Literal[
    "not_started",
    "approved",
    "changes_required",
    "review_failure",
]


ValidationStatus = Literal[
    "not_started",
    "environment_failure",
    "project_validation_failure",
    "validation_success",
]


class WorkflowState(TypedDict):
    issue_number: int
    issue_title: str
    issue_body: str

    plan: dict[str, Any]
    plan_markdown: str
    plan_published: bool
    planning_error: str
    requires_user_input: bool

    current_step: int

    workspace: str
    branch: str

    attempt: int
    max_attempts: int

    environment_output: str
    environment_ready: bool

    validation_status: ValidationStatus
    validation_exit_code: int
    test_output: str
    tests_passed: bool

    coder_summary: str
    coder_error: str

    review_status: ReviewStatus
    review: dict[str, Any]
    review_markdown: str
    review_published: bool
    review_error: str

    error: str
