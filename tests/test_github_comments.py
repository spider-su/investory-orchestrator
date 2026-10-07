from __future__ import annotations

import unittest
from datetime import datetime, timezone
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

    def test_latest_approval_requires_configured_reviewer_and_current_head(self) -> None:
        submitted = datetime(2026, 10, 7, tzinfo=timezone.utc)
        current = SimpleNamespace(
            id=2,
            user=SimpleNamespace(login="spider-su"),
            state="APPROVED",
            commit_id="head-sha",
            submitted_at=submitted,
        )
        stale = SimpleNamespace(
            id=1,
            user=SimpleNamespace(login="spider-su"),
            state="APPROVED",
            commit_id="old-sha",
            submitted_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        other_reviewer = SimpleNamespace(
            id=3,
            user=SimpleNamespace(login="other"),
            state="CHANGES_REQUESTED",
            commit_id="head-sha",
            submitted_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
        )
        pull = SimpleNamespace(
            head=SimpleNamespace(sha="head-sha"),
            get_reviews=Mock(return_value=[stale, current, other_reviewer]),
        )
        self.client.get_pull_request = Mock(return_value=pull)

        approval = self.client.get_latest_review_approval(13, "SPIDER-SU")

        self.assertEqual(approval["reviewer"], "SPIDER-SU")
        self.assertEqual(approval["state"], "APPROVED")
        self.assertEqual(approval["commit_sha"], "head-sha")
        self.assertEqual(approval["review_id"], "2")

    def test_merge_rechecks_approval_and_pins_head_sha(self) -> None:
        result = SimpleNamespace(merged=True, sha="merge-sha", message="Merged")
        pull = SimpleNamespace(
            state="open",
            merged=False,
            draft=False,
            head=SimpleNamespace(sha="head-sha"),
            merge=Mock(return_value=result),
        )
        self.client.get_pull_request = Mock(return_value=pull)
        self.client.get_latest_review_approval = Mock(return_value={
            "reviewer": "spider-su",
            "state": "APPROVED",
            "commit_sha": "head-sha",
            "current_head_sha": "head-sha",
            "review_id": "77",
            "submitted_at": "2026-10-07T12:00:00+00:00",
        })

        merged = self.client.merge_approved_pull_request(
            13,
            reviewer_login="spider-su",
            expected_head_sha="head-sha",
        )

        pull.merge.assert_called_once_with(merge_method="squash", sha="head-sha")
        self.assertEqual(merged["merge_commit_sha"], "merge-sha")

    def test_merge_rejects_stale_approval_without_calling_github_merge(self) -> None:
        pull = SimpleNamespace(
            state="open",
            merged=False,
            draft=False,
            head=SimpleNamespace(sha="head-sha"),
            merge=Mock(),
        )
        self.client.get_pull_request = Mock(return_value=pull)
        self.client.get_latest_review_approval = Mock(return_value={
            "reviewer": "spider-su",
            "state": "APPROVED",
            "commit_sha": "old-sha",
            "current_head_sha": "head-sha",
        })

        with self.assertRaisesRegex(RuntimeError, "lacks a current approval"):
            self.client.merge_approved_pull_request(
                13,
                reviewer_login="spider-su",
                expected_head_sha="head-sha",
            )

        pull.merge.assert_not_called()

    def test_open_promotion_pr_filters_existing_prs_by_base(self) -> None:
        development_pr = SimpleNamespace(
            number=11, base=SimpleNamespace(ref="main")
        )
        other_pr = SimpleNamespace(
            number=12, base=SimpleNamespace(ref="develop")
        )
        repository = SimpleNamespace(
            owner=SimpleNamespace(login="spider-su"),
            get_pulls=Mock(return_value=[other_pr, development_pr]),
        )
        self.client.get_repository = Mock(return_value=repository)

        self.assertEqual(
            self.client.find_open_pr_by_branch("develop", base="main"),
            development_pr,
        )


if __name__ == "__main__":
    unittest.main()
