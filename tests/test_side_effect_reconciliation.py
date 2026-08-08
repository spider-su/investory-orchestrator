from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from app.graph import (
    create_draft_pr_node,
    prepare_draft_pr_node,
    prepare_push_branch_node,
    push_branch_node,
)


def base_state() -> dict:
    return {
        "issue_number": 42,
        "issue_title": "Reconcile remote side effects",
        "workspace": "/tmp/issue-42",
        "branch": "agent/issue-42",
        "remote_baseline_sha": "old-sha",
        "final_commit_sha": "final-sha",
        "plan": {"summary": "Add reconciliation."},
        "steps": [
            {
                "id": "step-01",
                "title": "Add reconciliation",
                "status": "completed",
            }
        ],
        "side_effect_intent": {},
        "side_effect_history": [],
        "workflow_status": "publishing",
        "blocked_reason": "",
        "blocked_stage": "",
        "error": "",
    }


class FakePullRequest:
    def __init__(self) -> None:
        self.number = 9
        self.html_url = "https://example.test/pull/9"
        self.edits: list[dict[str, str]] = []

    def edit(self, *, title: str, body: str) -> None:
        self.edits.append({"title": title, "body": body})


class SideEffectReconciliationTests(unittest.TestCase):
    def test_prepare_push_records_remote_precondition(self) -> None:
        client = SimpleNamespace(
            get_branch_head_sha=lambda branch: "old-sha"
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            result = prepare_push_branch_node(base_state())

        intent = result["side_effect_intent"]
        self.assertEqual(intent["kind"], "push_branch")
        self.assertEqual(intent["status"], "prepared")
        self.assertEqual(intent["target_sha"], "final-sha")
        self.assertEqual(intent["expected_remote_sha"], "old-sha")

    def test_push_reconciles_already_applied_operation(self) -> None:
        state = base_state()
        state["side_effect_intent"] = {
            "operation_id": (
                "issue-42:push-branch:agent/issue-42:final-sha"
            ),
            "kind": "push_branch",
            "status": "prepared",
            "branch": "agent/issue-42",
            "target_sha": "final-sha",
            "expected_remote_sha": "old-sha",
        }
        client = SimpleNamespace(
            get_branch_head_sha=lambda branch: "final-sha"
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            with patch("app.graph.push_branch") as push_mock:
                result = push_branch_node(state)

        push_mock.assert_not_called()
        self.assertEqual(result["side_effect_intent"], {})
        self.assertEqual(
            result["side_effect_history"][0]["status"],
            "completed",
        )

    def test_push_blocks_when_remote_moved(self) -> None:
        state = base_state()
        state["side_effect_intent"] = {
            "operation_id": (
                "issue-42:push-branch:agent/issue-42:final-sha"
            ),
            "kind": "push_branch",
            "status": "prepared",
            "branch": "agent/issue-42",
            "target_sha": "final-sha",
            "expected_remote_sha": "old-sha",
        }
        client = SimpleNamespace(
            get_branch_head_sha=lambda branch: "human-sha"
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            with patch("app.graph.push_branch") as push_mock:
                result = push_branch_node(state)

        push_mock.assert_not_called()
        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(result["blocked_stage"], "push_branch")
        self.assertIn("moved", result["blocked_reason"])

    def test_prepare_push_blocks_when_branch_changed_since_workspace_start(self) -> None:
        client = SimpleNamespace(
            get_branch_head_sha=lambda branch: "human-sha"
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            result = prepare_push_branch_node(base_state())

        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(result["blocked_stage"], "prepare_push_branch")
        self.assertIn("since workspace preparation", result["blocked_reason"])

    def test_push_uses_recorded_lease(self) -> None:
        state = base_state()
        state["side_effect_intent"] = {
            "operation_id": (
                "issue-42:push-branch:agent/issue-42:final-sha"
            ),
            "kind": "push_branch",
            "status": "prepared",
            "branch": "agent/issue-42",
            "target_sha": "final-sha",
            "expected_remote_sha": "old-sha",
        }
        remote_values = iter(["old-sha", "final-sha"])
        client = SimpleNamespace(
            get_branch_head_sha=lambda branch: next(remote_values)
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            with patch("app.graph.push_branch") as push_mock:
                result = push_branch_node(state)

        push_mock.assert_called_once_with(
            client,
            ANY,
            "agent/issue-42",
            expected_local_sha="final-sha",
            expected_remote_sha="old-sha",
        )
        self.assertEqual(result["side_effect_intent"], {})

    def test_prepare_draft_pr_blocks_without_final_commit(self) -> None:
        state = base_state()
        state["final_commit_sha"] = None

        result = prepare_draft_pr_node(state)

        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(result["blocked_stage"], "prepare_draft_pr")
        self.assertIn("final commit SHA", result["blocked_reason"])

    def test_pr_retry_updates_existing_pr_instead_of_creating(self) -> None:
        state = base_state()
        prepared = prepare_draft_pr_node(state)
        state.update(prepared)
        pull_request = FakePullRequest()
        client = SimpleNamespace(
            find_open_pr_by_branch=lambda branch: pull_request,
            create_draft_pr=Mock(),
            update_pull_request=lambda pull_request, **kwargs: (
                pull_request.edit(**kwargs) or pull_request
            ),
        )

        with patch("app.graph.GitHubAppClient", return_value=client):
            result = create_draft_pr_node(state)

        client.create_draft_pr.assert_not_called()
        self.assertEqual(len(pull_request.edits), 1)
        self.assertIn(
            state["side_effect_intent"]["operation_id"],
            pull_request.edits[0]["body"],
        )
        self.assertEqual(result["pull_request_number"], 9)
        self.assertEqual(result["side_effect_intent"], {})


if __name__ == "__main__":
    unittest.main()
