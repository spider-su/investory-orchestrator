from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.graph import (
    awaiting_user_input_node,
    reload_issue_for_planning,
    resume_from_for_stage,
)


class UserInputResumeTests(unittest.TestCase):
    def test_open_questions_create_resumable_blocked_state(self) -> None:
        result = awaiting_user_input_node(
            {
                "plan": {
                    "open_questions": [
                        "Which compatibility behavior is required?",
                        "Should existing data be migrated?",
                    ]
                }
            }
        )

        self.assertEqual(result["workflow_status"], "blocked")
        self.assertEqual(
            result["blocked_stage"],
            "awaiting_user_input",
        )
        self.assertIn(
            "Which compatibility behavior is required?",
            result["blocked_reason"],
        )
        self.assertIn(
            "Should existing data be migrated?",
            result["blocked_reason"],
        )
        self.assertEqual(result["error"], "")

    def test_user_input_stage_resumes_before_context_collection(self) -> None:
        self.assertEqual(
            resume_from_for_stage("awaiting_user_input"),
            "prepare_workspace",
        )
        self.assertIsNone(resume_from_for_stage("unknown"))

    def test_reload_issue_resets_planning_state(self) -> None:
        issue = SimpleNamespace(
            number=42,
            title="Clarified title",
            body="Clarified requirements",
        )

        with patch("app.graph.GitHubAppClient") as client_class:
            client_class.return_value.get_issue.return_value = issue
            result = reload_issue_for_planning(42)

        client_class.return_value.get_issue.assert_called_once_with(42)
        self.assertEqual(result["issue_title"], "Clarified title")
        self.assertEqual(result["issue_body"], "Clarified requirements")
        self.assertEqual(result["workflow_status"], "planning")
        self.assertEqual(result["plan"], {})
        self.assertEqual(result["steps"], [])
        self.assertFalse(result["requires_user_input"])
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["blocked_stage"], "")

    def test_reload_issue_normalizes_missing_body(self) -> None:
        issue = SimpleNamespace(
            number=42,
            title="Clarified title",
            body=None,
        )

        with patch("app.graph.GitHubAppClient") as client_class:
            client_class.return_value.get_issue.return_value = issue
            result = reload_issue_for_planning(42)

        self.assertEqual(result["issue_body"], "")


if __name__ == "__main__":
    unittest.main()
