from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from github import Auth, Github, GithubIntegration
from github.GithubException import GithubException
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository


class GitHubAppClient:
    def __init__(self, repository_name: str | None = None) -> None:
        self.app_id = int(self._required_env("GITHUB_APP_ID"))
        self.installation_id = int(
            self._required_env("GITHUB_INSTALLATION_ID")
        )
        self.repository_name = repository_name or self._required_env("GITHUB_REPOSITORY")

        private_key_path = Path(
            self._required_env("GITHUB_PRIVATE_KEY_PATH")
        )

        if not private_key_path.is_file():
            raise RuntimeError(
                f"GitHub App private key not found: {private_key_path}"
            )

        private_key = private_key_path.read_text(encoding="utf-8")

        app_auth = Auth.AppAuth(
            app_id=self.app_id,
            private_key=private_key,
        )

        integration = GithubIntegration(auth=app_auth)

        try:
            installation_token = integration.get_access_token(
                self.installation_id
            )
        except GithubException as error:
            raise RuntimeError(
                "Failed to obtain GitHub App installation token: "
                f"{error.status} {error.data}"
            ) from error

        self.token = installation_token.token
        self.github = Github(auth=Auth.Token(self.token))

    def get_repository(self) -> Repository:
        try:
            return self.github.get_repo(self.repository_name)
        except GithubException as error:
            raise RuntimeError(
                f"Failed to access repository "
                f"'{self.repository_name}': "
                f"{error.status} {error.data}"
            ) from error

    def get_issue(self, issue_number: int) -> Issue:
        try:
            return self.get_repository().get_issue(
                number=issue_number
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to access issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def update_issue_body(self, issue_number: int, body: str) -> None:
        try:
            self.get_issue(issue_number).edit(body=body)
        except GithubException as error:
            raise RuntimeError(
                f"Failed to update issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def list_ready_issues(self, label: str) -> list[Issue]:
        """Return open issues carrying the execution-authorization label."""
        try:
            issues = self.get_repository().get_issues(
                state="open", labels=[label], sort="created", direction="asc"
            )
            return [issue for issue in issues if not getattr(issue, "pull_request", None)]
        except GithubException as error:
            raise RuntimeError(
                f"Failed to list issues with label '{label}': "
                f"{error.status} {error.data}"
            ) from error

    def remove_issue_label(self, issue_number: int, label: str) -> None:
        try:
            self.get_issue(issue_number).remove_from_labels(label)
        except GithubException as error:
            raise RuntimeError(
                f"Failed to remove label '{label}' from issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def create_issue(
        self,
        title: str,
        body: str,
    ) -> Issue:
        try:
            return self.get_repository().create_issue(
                title=title,
                body=body,
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to create issue: "
                f"{error.status} {error.data}"
            ) from error

    def add_issue_comment(
        self,
        issue_number: int,
        body: str,
    ) -> int:
        try:
            comment = self.get_issue(issue_number).create_comment(body)
            return comment.id
        except GithubException as error:
            raise RuntimeError(
                f"Failed to comment on issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def upsert_issue_comment(
        self,
        issue_number: int,
        body: str,
        *,
        marker: str,
    ) -> int:
        try:
            issue = self.get_issue(issue_number)
            matches = [
                comment
                for comment in issue.get_comments()
                if marker in (comment.body or "")
            ]

            if len(matches) > 1:
                raise RuntimeError(
                    f"Multiple issue comments contain marker '{marker}' "
                    f"for issue #{issue_number}"
                )

            if matches:
                matches[0].edit(body)
                return matches[0].id

            comment = issue.create_comment(body)
            return comment.id
        except GithubException as error:
            raise RuntimeError(
                f"Failed to upsert comment on issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def get_branch_head_sha(
        self,
        branch_name: str,
    ) -> str | None:
        try:
            branch = self.get_repository().get_branch(branch_name)
            return branch.commit.sha
        except GithubException as error:
            if error.status == 404:
                return None

            raise RuntimeError(
                f"Failed to inspect branch '{branch_name}': "
                f"{error.status} {error.data}"
            ) from error

    def create_branch(
        self,
        branch_name: str,
        base_branch: str = "main",
    ) -> str:
        repository = self.get_repository()

        try:
            base = repository.get_branch(base_branch)

            reference = repository.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=base.commit.sha,
            )

            return reference.ref
        except GithubException as error:
            raise RuntimeError(
                f"Failed to create branch '{branch_name}' "
                f"from '{base_branch}': "
                f"{error.status} {error.data}"
            ) from error

    def delete_branch(self, branch_name: str) -> None:
        repository = self.get_repository()

        try:
            reference = repository.get_git_ref(
                f"heads/{branch_name}"
            )
            reference.delete()
        except GithubException as error:
            raise RuntimeError(
                f"Failed to delete branch '{branch_name}': "
                f"{error.status} {error.data}"
            ) from error

    def create_draft_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> PullRequest:
        try:
            return self.get_repository().create_pull(
                title=title,
                body=body,
                head=head,
                base=base,
                draft=True,
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to create draft pull request "
                f"from '{head}' to '{base}': "
                f"{error.status} {error.data}"
            ) from error

    def update_pull_request(
        self,
        pull_request: PullRequest,
        *,
        title: str,
        body: str,
    ) -> PullRequest:
        try:
            pull_request.edit(
                title=title,
                body=body,
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to update pull request "
                f"#{pull_request.number}: "
                f"{error.status} {error.data}"
            ) from error

        return pull_request

    def mark_pull_request_ready(self, pull_request_number: int) -> None:
        """Promote a draft PR after all automated review and CI gates pass."""
        try:
            pull_request = self.get_pull_request(pull_request_number)
            if pull_request.draft:
                pull_request.mark_ready_for_review()
        except GithubException as error:
            raise RuntimeError(
                f"Failed to mark pull request #{pull_request_number} ready: "
                f"{error.status} {error.data}"
            ) from error

    def find_open_pr_by_branch(
        self,
        branch: str,
        *,
        base: str | None = None,
    ) -> PullRequest | None:
        repository = self.get_repository()
        owner = repository.owner.login

        try:
            pull_requests = repository.get_pulls(
                state="open",
                head=f"{owner}:{branch}",
            )

            matches = [
                pull_request
                for pull_request in pull_requests
                if base is None or pull_request.base.ref == base
            ]

            if len(matches) > 1:
                raise RuntimeError(
                    f"Multiple open pull requests found for branch "
                    f"'{branch}'"
                )

            if matches:
                return matches[0]
        except GithubException as error:
            raise RuntimeError(
                f"Failed to search for an open pull request "
                f"for branch '{branch}': "
                f"{error.status} {error.data}"
            ) from error

        return None

    def get_latest_review_approval(
        self,
        pull_request_number: int,
        reviewer_login: str,
    ) -> dict[str, str] | None:
        """Return that reviewer's latest submitted review for the current PR head."""
        try:
            pull_request = self.get_pull_request(pull_request_number)
            current_head = pull_request.head.sha
            reviews = [
                review
                for review in pull_request.get_reviews()
                if (getattr(getattr(review, "user", None), "login", "") or "").casefold()
                == reviewer_login.casefold()
                and getattr(review, "submitted_at", None) is not None
            ]
            if not reviews:
                return None
            latest = max(reviews, key=lambda review: (review.submitted_at, review.id))
            return {
                "reviewer": reviewer_login,
                "state": (latest.state or "").upper(),
                "commit_sha": latest.commit_id or "",
                "current_head_sha": current_head,
                "review_id": str(latest.id),
                "submitted_at": latest.submitted_at.isoformat(),
            }
        except GithubException as error:
            raise RuntimeError(
                f"Failed to inspect reviews for pull request "
                f"#{pull_request_number}: {error.status} {error.data}"
            ) from error

    def merge_approved_pull_request(
        self,
        pull_request_number: int,
        *,
        reviewer_login: str,
        expected_head_sha: str,
        merge_method: str = "squash",
    ) -> dict[str, str]:
        """Merge only the reviewed head, rechecking approval immediately before merge."""
        if merge_method not in {"merge", "squash", "rebase"}:
            raise ValueError("merge_method must be merge, squash, or rebase")
        pull_request = self.get_pull_request(pull_request_number)
        if pull_request.state != "open" or pull_request.merged:
            raise RuntimeError(f"Pull request #{pull_request_number} is not open")
        if pull_request.draft:
            raise RuntimeError(f"Pull request #{pull_request_number} is still a draft")
        if pull_request.head.sha != expected_head_sha:
            raise RuntimeError("Pull request head changed after final review")
        approval = self.get_latest_review_approval(
            pull_request_number, reviewer_login
        )
        if (
            approval is None
            or approval["state"] != "APPROVED"
            or approval["commit_sha"] != expected_head_sha
            or approval["current_head_sha"] != expected_head_sha
        ):
            raise RuntimeError(
                f"Pull request #{pull_request_number} lacks a current approval "
                f"from @{reviewer_login}"
            )
        try:
            result = pull_request.merge(
                merge_method=merge_method,
                sha=expected_head_sha,
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to merge pull request #{pull_request_number}: "
                f"{error.status} {error.data}"
            ) from error
        if not result.merged:
            raise RuntimeError(
                f"GitHub did not merge pull request #{pull_request_number}: "
                f"{result.message}"
            )
        return {**approval, "merge_commit_sha": result.sha or ""}

    def create_release_promotion_pr(
        self,
        *,
        head: str,
        base: str,
    ) -> PullRequest:
        """Open a ready-for-review PR to promote accumulated branch changes."""
        try:
            return self.get_repository().create_pull(
                title=f"Promote {head} to {base}",
                body=(
                    f"Promote all changes currently on `{head}` to `{base}`.\n\n"
                    "This release PR is opened after an orchestrated task has "
                    "merged into the development branch and passed post-merge CI. "
                    "Review the accumulated diff and merge manually when the "
                    "release is ready."
                ),
                head=head,
                base=base,
                draft=False,
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to create release promotion PR from '{head}' to '{base}': "
                f"{error.status} {error.data}"
            ) from error

    def get_pull_request(
        self,
        pull_request_number: int,
    ) -> PullRequest:
        try:
            return self.get_repository().get_pull(
                pull_request_number
            )
        except GithubException as error:
            raise RuntimeError(
                f"Failed to access pull request "
                f"#{pull_request_number}: "
                f"{error.status} {error.data}"
            ) from error

    def get_pull_request_details(self, pull_request_number: int) -> dict[str, Any]:
        pull = self.get_pull_request(pull_request_number)
        merge_commit = getattr(pull, "merge_commit_sha", None)
        merged_by = getattr(pull, "merged_by", None)
        merged_at = getattr(pull, "merged_at", None)
        return {
            "number": pull.number,
            "url": pull.html_url,
            "state": pull.state,
            "is_merged": bool(pull.merged),
            "is_draft": bool(pull.draft),
            "base_ref": pull.base.ref,
            "head_ref": pull.head.ref,
            "head_sha": pull.head.sha,
            "merge_commit_sha": merge_commit,
            "merged_at": merged_at.isoformat() if merged_at else "",
            "merged_by": getattr(merged_by, "login", "") if merged_by else "",
            "title": pull.title,
            "body": pull.body or "",
        }

    @staticmethod
    def pull_request_closes_issue(details: dict[str, Any], issue_number: int) -> bool:
        import re

        body = details.get("body", "")
        return bool(re.search(
            rf"(?im)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+"
            rf"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#{issue_number}\b",
            body,
        ))

    def close_issue(self, issue_number: int) -> bool:
        try:
            issue = self.get_issue(issue_number)
            if issue.state == "closed":
                return False
            issue.edit(state="closed")
            return True
        except GithubException as error:
            raise RuntimeError(
                f"Failed to close issue #{issue_number}: "
                f"{error.status} {error.data}"
            ) from error

    def get_pull_request_ci(
        self,
        pull_request_number: int | None,
    ) -> tuple[str, list[dict[str, str]]]:
        """Return pending, success, or failure for the PR head's checks."""
        if pull_request_number is None:
            raise RuntimeError("Task has no pull request to inspect for CI.")
        pull = self.get_pull_request(pull_request_number)
        return self.get_commit_ci(pull.head.sha)

    def get_commit_ci(
        self,
        commit_sha: str,
    ) -> tuple[str, list[dict[str, str]]]:
        repository = self.get_repository()
        try:
            commit = repository.get_commit(commit_sha)
        except GithubException as error:
            raise RuntimeError(
                f"Failed to access commit {commit_sha}: "
                f"{error.status} {error.data}"
            ) from error
        results: list[dict[str, str]] = []
        pending = False
        failed = False

        try:
            check_runs = commit.get_check_runs()
        except GithubException as error:
            raise RuntimeError(
                f"Failed to inspect CI for commit {commit_sha}: "
                f"{error.status} {error.data}"
            ) from error
        for check in check_runs:
            conclusion = (check.conclusion or "").lower()
            status = (check.status or "").lower()
            output = getattr(check, "output", None)
            results.append({
                "name": check.name,
                "status": status,
                "conclusion": conclusion,
                "url": check.html_url or "",
                "output": "\n".join(
                    part
                    for part in (
                        getattr(output, "title", ""),
                        getattr(output, "summary", ""),
                        getattr(output, "text", ""),
                    )
                    if part
                )[-20_000:],
            })
            if status != "completed":
                pending = True
            elif conclusion not in {"success", "skipped", "neutral"}:
                failed = True

        try:
            combined = commit.get_combined_status()
        except GithubException as error:
            raise RuntimeError(
                f"Failed to inspect commit status for {commit_sha}: "
                f"{error.status} {error.data}"
            ) from error
        for item in combined.statuses:
            state = (item.state or "").lower()
            results.append({
                "name": item.context,
                "status": state,
                "conclusion": state,
                "url": item.target_url or "",
            })
            if state == "pending":
                pending = True
            elif state not in {"success", ""}:
                failed = True

        if failed:
            return "failure", results
        if pending or not results:
            return "pending", results
        return "success", results

    def add_pull_request_comment(
        self,
        pull_request_number: int,
        body: str,
    ) -> int:
        try:
            pull_request = self.get_pull_request(
                pull_request_number
            )

            comment = pull_request.create_issue_comment(body)
            return comment.id
        except GithubException as error:
            raise RuntimeError(
                f"Failed to comment on pull request "
                f"#{pull_request_number}: "
                f"{error.status} {error.data}"
            ) from error

    def close_pull_request(
        self,
        pull_request_number: int,
    ) -> None:
        try:
            pull_request = self.get_pull_request(
                pull_request_number
            )
            pull_request.edit(state="closed")
        except GithubException as error:
            raise RuntimeError(
                f"Failed to close pull request "
                f"#{pull_request_number}: "
                f"{error.status} {error.data}"
            ) from error

    def get_open_issue_count(self) -> int:
        return self.get_repository().get_issues(
            state="open"
        ).totalCount

    def get_repository_info(self) -> dict[str, Any]:
        repository = self.get_repository()

        return {
            "full_name": repository.full_name,
            "default_branch": repository.default_branch,
            "private": repository.private,
            "open_issues": repository.open_issues_count,
        }

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.getenv(name)

        if not value:
            raise RuntimeError(
                f"Required environment variable is missing: {name}"
            )

        return value
