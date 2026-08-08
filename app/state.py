from __future__ import annotations

from typing import Any, Optional, TypedDict

from typing_extensions import TypeAlias


WorkflowStatus: TypeAlias = str
ValidationStatus: TypeAlias = str
ReviewStatus: TypeAlias = str
BlockedStage: TypeAlias = str
CiStatus: TypeAlias = str


class WorkflowState(TypedDict):
    issue_number: int
    issue_title: str
    issue_body: str
    repository_context: str

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

    issue_baseline_sha: str
    remote_baseline_sha: str
    checkpoint_commits: list[str]

    attempt: int
    max_attempts: int
    step_baseline_sha: str
    attempt_artifacts: list[dict[str, Any]]
    last_failed_patch_path: str

    final_baseline_sha: str
    final_attempt: int
    max_final_attempts: int
    last_failed_final_patch_path: str
    final_validation_status: ValidationStatus
    final_validation_exit_code: int
    final_validation_output: str
    final_review_status: ReviewStatus
    final_review: dict[str, Any]
    final_review_error: str
    final_commit_sha: Optional[str]

    coder_summary: str
    coder_error: str
    coder_backend: str
    coder_provider: str
    coder_model: str

    environment_ready: bool
    environment_output: str
    cleanup_status: str
    cleanup_output: str
    cleanup_resume_stage: str
    cleanup_resume_reason: str

    validation_status: ValidationStatus
    validation_exit_code: int
    test_output: str
    tests_passed: bool

    review_status: ReviewStatus
    review: dict[str, Any]
    review_markdown: str
    review_published: bool
    review_error: str
    reviewer_backend: str
    reviewer_provider: str
    reviewer_model: str
    review_independence: str
    review_context_fresh: bool
    review_read_only: bool

    commit_sha: Optional[str]

    pull_request_number: int
    pull_request_url: str

    side_effect_intent: dict[str, Any]
    side_effect_history: list[dict[str, Any]]

    ci_status: CiStatus
    ci_run_id: int
    ci_url: str
    ci_output: str

    blocked_reason: str
    blocked_stage: BlockedStage
    error: str
