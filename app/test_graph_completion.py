from __future__ import annotations

import unittest
from unittest.mock import patch

from app.graph import (
    complete_step_node,
    create_draft_pr_node,
    push_branch_node,
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

    def find_open_pr_by_branch(self, branch: str):
        self.last_branch = branch
        return self.open_pr

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

    def test_push_branch_node_returns_clear_state_on_success(self) -> None:
        fake_client = object()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch("app.graph.push_branch") as push_branch_mock:
                result = push_branch_node(build_state())

        push_branch_mock.assert_called_once()
        self.assertEqual(
            result,
            {
                "blocked_reason": "",
                "blocked_stage": "",
                "error": "",
            },
        )

    def test_push_branch_node_blocks_on_runtime_error(self) -> None:
        with patch("app.graph.GitHubAppClient", return_value=object()):
            with patch(
                "app.graph.push_branch",
                side_effect=RuntimeError("push failed"),
            ):
                result = push_branch_node(build_state())

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
        fake_client = FakeGitHubClient()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            result = create_draft_pr_node(build_state())

        self.assertEqual(len(fake_client.created), 1)
        created = fake_client.created[0]
        self.assertEqual(created["head"], "agent/issue-42")
        self.assertEqual(created["base"], "main")
        self.assertIn("Closes #42", created["body"])
        self.assertIn("- [x] step-01: Add agent tests", created["body"])
        self.assertIn("Final commit: `abc123`", created["body"])
        self.assertEqual(
            result,
            {
                "workflow_status": "completed",
                "pull_request_number": 7,
                "pull_request_url": "https://example/pr/7",
                "blocked_reason": "",
                "blocked_stage": "",
                "error": "",
            },
        )

    def test_create_draft_pr_node_updates_existing_pr(self) -> None:
        fake_client = FakeGitHubClient()
        fake_client.open_pr = FakePullRequest(number=9, url="https://example/pr/9")

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            result = create_draft_pr_node(build_state())

        self.assertEqual(fake_client.last_branch, "agent/issue-42")
        self.assertEqual(fake_client.created, [])
        self.assertEqual(len(fake_client.open_pr.edits), 1)
        self.assertEqual(result["pull_request_number"], 9)
        self.assertEqual(result["pull_request_url"], "https://example/pr/9")

    def test_create_draft_pr_node_blocks_on_runtime_error(self) -> None:
        fake_client = FakeGitHubClient()

        with patch("app.graph.GitHubAppClient", return_value=fake_client):
            with patch.object(
                fake_client,
                "find_open_pr_by_branch",
                side_effect=RuntimeError("GitHub API failed"),
            ):
                result = create_draft_pr_node(build_state())

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

