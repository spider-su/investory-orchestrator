from __future__ import annotations

import unittest
from unittest.mock import patch

from github.GithubException import GithubException

from app.github_client import GitHubAppClient

from app.graph import (
    coder_node,
    cleanup_node,
    complete_step_node,
    create_draft_pr_node,
    push_branch_node,
    resolve_resume_from,
    route_after_validation,
    run_validation_node,
)
from app.agents.coder import CoderError
from app.side_effects import (
    prepare_draft_pr_intent,
    prepare_push_intent,
)


def build_state() -> dict:
    return {
        "issue_number": 42,
        "issue_title": "Add tests for agents",
        "workspace": "D:/projects/investory-orchestrator/workspaces/issue-42",
        "branch": "agent/issue-42",
        "steps": [
            {
                "id": "step-01",
                "title": "Add agent tests",
                "status": "completed",
            },
            {
                "id": "step-02",
                "title": "Validate routing",
                "status": "pending",
            },
        ],
        "plan": {"summary": "Add agent test coverage."},
        "final_commit_sha": "abc123",
        "workflow_status": "implementing",
        "side_effect_intent": {},
        "side_effect_history": [],
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


class FakePullRequest:
    def __init__(self, number: int = 7, url: str = "https://example/pr/7"):
        self.number = number
        self.html_url = url
        self.edits: list[dict[str, str]] = []

    def edit(self, *, title: str, body: str) -> None:
        self.edits.append({"title": title, "body": body})


class FakeGitHubClient:
    def __init__(self):
        self.open_pr = None
        self.created: list[dict[str, str]] = []
        self.branch_heads: dict[str, str] = {}

    def find_open_pr_by_branch(self, branch: str):
        self.last_branch = branch
        return self.open_pr

    def get_branch_head_sha(self, branch: str) -> str | None:
        return self.branch_heads.get(branch)

    def update_pull_request(
        self,
        pull_request: FakePullRequest,
        *,
        title: str,
        body: str,
    ) -> FakePullRequest:
        pull_request.edit(title=title, body=body)
        return pull_request

    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> FakePullRequest:
        self.created.append(
            {
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            }
        )
        return FakePullRequest()


class GraphCompletionTests(unittest.TestCase):
    def test_coder_failure_persists_consumed_attempt(self) -> None:
        state = {
            "issue_number": 42,
            "issue_title": "Attempt accounting",
            "issue_body": "",
            "workspace": "D:/projects/investory-orchestrator",
            "steps": [{"id": "step-01", "title": "Implement"}],
            "current_step": 0,
            "attempt": 1,
            "max_attempts": 3,
            "test_output": "",
            "review": {},
            "last_failed_patch_path": "",
            "attempt_artifacts": [],
        }

        with patch(
            "app.graph.run_coder",
            side_effect=CoderError("coder failed after editing"),
        ):
            with patch("app.graph.workspace_has_changes", return_value=True):
                with patch(
                    "app.graph.archive_and_reset_failed_attempt",
                    return_value={"patch_path": "attempt-2.patch"},
                ) as archive_mock:
                    result = coder_node(state)

        archive_mock.assert_called_once()
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(
            result["attempt_artifacts"],
            [{"patch_path": "attempt-2.patch"}],
        )
        self.assertEqual(
            resolve_resume_from(
                {
                    "workflow_status": "blocked",
                    "blocked_stage": "coder",
                    "attempt": result["attempt"],
                }
            ),
            "prepare_current_step",
        )

    def test_validation_environment_failure_blocks_without_retry(self) -> None:
        for exit_code, output in (
            (127, "validation command missing"),
            (124, "validation timed out"),
        ):
            with self.subTest(exit_code=exit_code):
                state = {
                    "workspace": "D:/projects/investory-orchestrator",
                    "issue_number": 42,
                    "attempt": 2,
                }
                with patch(
                    "app.graph.run_validation",
                    return_value={
                        "status": "environment_failure",
                        "success": False,
                        "exit_code": exit_code,
                        "output": output,
                    },
                ):
                    result = run_validation_node(state)

                self.assertEqual(
                    result["validation_status"],
                    "environment_failure",
                )
                self.assertEqual(result["blocked_stage"], "environment")
                self.assertEqual(result["test_output"], output)
                self.assertEqual(
                    route_after_validation(result),
                    "environment_failure",
                )
                self.assertEqual(state["attempt"], 2)

    def test_cleanup_failure_is_persisted_as_blocked_state(self) -> None:
        with patch(
            "app.graph.stop_environment",
            return_value={
                "success": False,
                "output": "container still running",
            },
        ):
            result = cleanup_node(
                {
                    "workspace": "/tmp/issue-42",
                    "issue_number": 42,
                    "blocked_reason": "Original failure",
                }
            )

        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(result["blocked_stage"], "cleanup")
        self.assertEqual(result["cleanup_status"], "failure")
        self.assertIn("Original failure", result["blocked_reason"])
        self.assertIn("container still running", result["blocked_reason"])

    def test_complete_step_node_allows_noop_commit(self) -> None:
        state = {
            "workspace": "D:/projects/investory-orchestrator/workspaces/issue-42",
            "current_step": 0,
            "attempt": 2,
            "completed_steps": [],
            "steps": [
                {
                    "id": "step-01",
                    "title": "Add agent tests",
                    "status": "in_progress",
                    "attempts": 0,
                    "commit_sha": None,
                }
            ],
        }

        with patch("app.graph.commit_step", return_value=None):
            result = complete_step_node(state)

        self.assertEqual(result["commit_sha"], None)
        self.assertEqual(result["current_step"], 1)
        self.assertEqual(result["completed_steps"], ["step-01"])
        self.assertEqual(result["steps"][0]["status"], "completed")
        self.assertEqual(result["steps"][0]["attempts"], 2)
        self.assertEqual(result["steps"][0]["commit_sha"], None)

    def test_github_client_normalizes_pr_update_error(self) -> None:
        client = GitHubAppClient.__new__(GitHubAppClient)
        pull_request = FakePullRequest(number=9)

        with patch.object(
            pull_request,
            "edit",
            side_effect=GithubException(422, {"message": "invalid"}),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Failed to update pull request #9",
            ):
                client.update_pull_request(
                    pull_request,
                    title="Updated title",
                    body="Updated body",
                )

    def test_push_branch_node_returns_clear_state_on_success(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_push_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
            expected_remote_sha=None,
        )
        fake_client = FakeGitHubClient()

        def record_push(*args, **kwargs) -> None:
            fake_client.branch_heads["agent/issue-42"] = "abc123"

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch(
                "app.graph.push_branch",
                side_effect=record_push,
            ) as push_branch_mock:
                result = push_branch_node(state)

        push_branch_mock.assert_called_once()
        self.assertEqual(result["side_effect_intent"], {})
        self.assertEqual(
            result["side_effect_history"][0]["status"],
            "completed",
        )
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["blocked_stage"], "")
        self.assertEqual(result["error"], "")

    def test_push_branch_node_blocks_on_runtime_error(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_push_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
            expected_remote_sha=None,
        )
        fake_client = FakeGitHubClient()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch(
                "app.graph.push_branch",
                side_effect=RuntimeError("push failed"),
            ):
                result = push_branch_node(state)

        self.assertEqual(
            result,
            {
                "workflow_status": "blocked",
                "blocked_reason": "push failed",
                "blocked_stage": "push_branch",
                "error": "push failed",
            },
        )

    def test_create_draft_pr_node_creates_pr_and_marks_completed(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_draft_pr_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
        )
        fake_client = FakeGitHubClient()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            result = create_draft_pr_node(state)

        self.assertEqual(len(fake_client.created), 1)
        created = fake_client.created[0]
        self.assertEqual(created["head"], "agent/issue-42")
        self.assertEqual(created["base"], "main")
        self.assertIn("Closes #42", created["body"])
        self.assertIn("- [x] step-01: Add agent tests", created["body"])
        self.assertIn("Final commit: `abc123`", created["body"])
        self.assertIn(
            state["side_effect_intent"]["operation_id"],
            created["body"],
        )
        self.assertEqual(result["workflow_status"], "completed")
        self.assertEqual(result["pull_request_number"], 7)
        self.assertEqual(result["pull_request_url"], "https://example/pr/7")
        self.assertEqual(result["side_effect_intent"], {})
        self.assertEqual(len(result["side_effect_history"]), 1)
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["blocked_stage"], "")
        self.assertEqual(result["error"], "")

    def test_create_draft_pr_node_updates_existing_pr(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_draft_pr_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
        )
        fake_client = FakeGitHubClient()
        fake_client.open_pr = FakePullRequest(number=9, url="https://example/pr/9")

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            result = create_draft_pr_node(state)

        self.assertEqual(fake_client.last_branch, "agent/issue-42")
        self.assertEqual(fake_client.created, [])
        self.assertEqual(len(fake_client.open_pr.edits), 1)
        self.assertEqual(result["pull_request_number"], 9)
        self.assertEqual(result["pull_request_url"], "https://example/pr/9")

    def test_create_draft_pr_node_blocks_on_update_error(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_draft_pr_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
        )
        fake_client = FakeGitHubClient()
        fake_client.open_pr = FakePullRequest(
            number=9,
            url="https://example/pr/9",
        )

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch.object(
                fake_client,
                "update_pull_request",
                side_effect=RuntimeError("PR update failed"),
            ):
                result = create_draft_pr_node(state)

        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(result["blocked_stage"], "create_draft_pr")
        self.assertEqual(result["blocked_reason"], "PR update failed")
        self.assertEqual(result["error"], "PR update failed")

    def test_create_draft_pr_node_blocks_on_runtime_error(self) -> None:
        state = build_state()
        state["side_effect_intent"] = prepare_draft_pr_intent(
            issue_number=42,
            branch="agent/issue-42",
            target_sha="abc123",
        )
        fake_client = FakeGitHubClient()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch.object(
                fake_client,
                "find_open_pr_by_branch",
                side_effect=RuntimeError("GitHub API failed"),
            ):
                result = create_draft_pr_node(state)

        self.assertEqual(
            result,
            {
                "workflow_status": "blocked",
                "blocked_reason": "GitHub API failed",
                "blocked_stage": "create_draft_pr",
                "error": "GitHub API failed",
            },
        )


if __name__ == "__main__":
    unittest.main()

