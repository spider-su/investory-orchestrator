from typing import Literal, TypedDict


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

    error: str
