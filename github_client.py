client = GitHubAppClient()

repo = client.repo()
issue = client.issue(42)

client.comment(42, "Planner started")
client.create_branch("agent/issue-42")
