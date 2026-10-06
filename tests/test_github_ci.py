from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.github_client import GitHubAppClient


class PullRequestCiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(GitHubAppClient)
        self.pull = SimpleNamespace(head=SimpleNamespace(sha="head-sha"))
        self.commit = SimpleNamespace(
            get_check_runs=Mock(return_value=[]),
            get_combined_status=Mock(
                return_value=SimpleNamespace(statuses=[])
            ),
        )
        self.client.get_pull_request = Mock(return_value=self.pull)
        self.client.get_repository = Mock(
            return_value=SimpleNamespace(
                get_commit=Mock(return_value=self.commit)
            )
        )

    def test_no_checks_are_pending_not_green(self) -> None:
        status, details = self.client.get_pull_request_ci(7)

        self.assertEqual(status, "pending")
        self.assertEqual(details, [])

    def test_all_successful_checks_are_green(self) -> None:
        check = SimpleNamespace(
            name="unit tests",
            status="completed",
            conclusion="success",
            html_url="https://example.test/check/1",
            output=SimpleNamespace(title="", summary="passed", text=""),
        )
        self.commit.get_check_runs.return_value = [check]

        status, details = self.client.get_pull_request_ci(7)

        self.assertEqual(status, "success")
        self.assertEqual(details[0]["output"], "passed")

    def test_failed_check_is_failure(self) -> None:
        check = SimpleNamespace(
            name="unit tests",
            status="completed",
            conclusion="failure",
            html_url="https://example.test/check/1",
            output=SimpleNamespace(title="tests failed", summary="trace", text=""),
        )
        self.commit.get_check_runs.return_value = [check]

        status, details = self.client.get_pull_request_ci(7)

        self.assertEqual(status, "failure")
        self.assertIn("trace", details[0]["output"])

    def test_pull_request_closing_keyword_links_issue(self) -> None:
        self.assertTrue(GitHubAppClient.pull_request_closes_issue(
            {"body": "Resolves spider-su/investory#104"}, 104
        ))
        self.assertFalse(GitHubAppClient.pull_request_closes_issue(
            {"body": "Related to #104"}, 104
        ))


if __name__ == "__main__":
    unittest.main()
