from __future__ import annotations

import sys

from app.github_client import GitHubAppClient


def main() -> None:
    client = GitHubAppClient()
    repo = client.get_repository()

    print("Authentication: OK")
    print(f"Repository: {repo.full_name}")
    print(f"Default branch: {repo.default_branch}")
    print(f"Private: {repo.private}")
    print(f"Open issues: {repo.get_issues(state='open').totalCount}")

    if len(sys.argv) == 2:
        issue_number = int(sys.argv[1])
        issue = repo.get_issue(number=issue_number)

        comment = issue.create_comment(
            "GitHub App write test: issue comment access works."
        )

        print(f"Issue #{issue_number}: {issue.title}")
        print(f"Comment write: OK — comment ID {comment.id}")


if __name__ == "__main__":
    main()
