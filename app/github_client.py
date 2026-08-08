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
    def __init__(self) -> None:
        self.app_id = int(self._required_env("GITHUB_APP_ID"))
        self.installation_id = int(
            self._required_env("GITHUB_INSTALLATION_ID")
        )
        self.repository_name = self._required_env("GITHUB_REPOSITORY")

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

    def find_open_pr_by_branch(
        self,
        branch: str,
    ) -> PullRequest | None:
        repository = self.get_repository()
        owner = repository.owner.login

        try:
            pull_requests = repository.get_pulls(
                state="open",
                head=f"{owner}:{branch}",
            )

            for pull_request in pull_requests:
                return pull_request
        except GithubException as error:
            raise RuntimeError(
                f"Failed to search for an open pull request "
                f"for branch '{branch}': "
                f"{error.status} {error.data}"
            ) from error

        return None

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
