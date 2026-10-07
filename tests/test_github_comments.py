from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.github_client import GitHubAppClient


class GitHubCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(GitHubAppClient)

    def test_ready_issue_query_excludes_pull_requests(self) -> None:
        issue = SimpleNamespace(number=1, pull_request=None)
        pull_request = SimpleNamespace(number=2, pull_request={"url": "example"})
        repository = SimpleNamespace(
            get_issues=Mock(return_value=[issue, pull_request])
        )
        self.client.get_repository = Mock(return_value=repository)

        self.assertEqual(self.client.list_ready_issues("ready_to_develop"), [issue])
        repository.get_issues.assert_called_once_with(
            state="open", labels=["ready_to_develop"], sort="created", direction="asc"
        )

    def test_ready_for_review_only_promotes_draft_pr(self) -> None:
        pull_request = SimpleNamespace(
            draft=True, mark_ready_for_review=Mock()
        )
        self.client.get_pull_request = Mock(return_value=pull_request)

        self.client.mark_pull_request_ready(13)

        pull_request.mark_ready_for_review.assert_called_once_with()

        pull_request.draft = False
        pull_request.mark_ready_for_review.reset_mock()
        self.client.mark_pull_request_ready(13)
        pull_request.mark_ready_for_review.assert_not_called()

    def test_upsert_updates_the_marked_comment(self) -> None:
        comment = SimpleNamespace(
            id=7,
            body="<!-- marker -->\nold",
            edit=Mock(),
        )
        issue = SimpleNamespace(
            get_comments=Mock(return_value=[comment]),
            create_comment=Mock(),
        )
        self.client.get_issue = Mock(return_value=issue)

        result = self.client.upsert_issue_comment(
            42,
            "<!-- marker -->\nnew",
            marker="<!-- marker -->",
        )

        self.assertEqual(result, 7)
        comment.edit.assert_called_once_with("<!-- marker -->\nnew")
        issue.create_comment.assert_not_called()

    def test_upsert_creates_when_no_marked_comment_exists(self) -> None:
        created = SimpleNamespace(id=8)
        issue = SimpleNamespace(
            get_comments=Mock(return_value=[]),
            create_comment=Mock(return_value=created),
        )
        self.client.get_issue = Mock(return_value=issue)

        result = self.client.upsert_issue_comment(
            42,
            "<!-- marker -->\nnew",
            marker="<!-- marker -->",
        )

        self.assertEqual(result, 8)
        issue.create_comment.assert_called_once_with("<!-- marker -->\nnew")

    def test_upsert_rejects_duplicate_markers(self) -> None:
        issue = SimpleNamespace(
            get_comments=Mock(
                return_value=[
                    SimpleNamespace(body="<!-- marker --> one"),
                    SimpleNamespace(body="<!-- marker --> two"),
                ]
            ),
            create_comment=Mock(),
        )
        self.client.get_issue = Mock(return_value=issue)

        with self.assertRaisesRegex(RuntimeError, "Multiple issue comments"):
            self.client.upsert_issue_comment(
                42,
                "<!-- marker -->\nnew",
                marker="<!-- marker -->",
            )

    def test_find_open_pr_rejects_duplicate_pull_requests(self) -> None:
        repository = SimpleNamespace(
            owner=SimpleNamespace(login="owner"),
            get_pulls=Mock(
                return_value=[
                    SimpleNamespace(number=1),
                    SimpleNamespace(number=2),
                ]
            ),
        )
        self.client.get_repository = Mock(return_value=repository)

        with self.assertRaisesRegex(RuntimeError, "Multiple open"):
            self.client.find_open_pr_by_branch("agent/issue-42")


if __name__ == "__main__":
    unittest.main()
