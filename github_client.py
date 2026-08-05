from __future__ import annotations

from app.github_client import GitHubAppClient


def main() -> None:
	client = GitHubAppClient()
	repository = client.get_repository()

	issue_number = 42
	issue = client.get_issue(issue_number)

	print(f"Repository: {repository.full_name}")
	print(f"Issue #{issue_number}: {issue.title}")

	comment_id = client.add_issue_comment(issue_number, "Planner started")
	print(f"Comment created: {comment_id}")


if __name__ == "__main__":
	main()
