from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.planner import ImplementationPlan, PlanStep
from app.agents.reviewer import ReviewResult
from app.cli import run_cli
from app.graph import build_graph, resolve_resume_from, reload_issue_for_planning
from app.task_scheduler import _notify_terminal_tasks, _poll_ci
from app.tasks import TaskStatus, TaskStore


class FakePullRequest:
    number = 23
    html_url = "https://example.test/pull/23"


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.pull_requests: list[dict[str, str]] = []
        self.branch_heads: dict[str, str] = {}
        self.ci_state = "success"
        self.ready_prs: list[int] = []
        self.approval: dict[str, str] | None = None
        self.merged = False
        self.merge_calls: list[dict[str, str]] = []
        self.release_prs: list[dict[str, str]] = []
        self.closed_issues: list[int] = []

    def get_issue(self, issue_number: int):
        return SimpleNamespace(
            number=issue_number,
            title="Integration fixture",
            body="Implement the deterministic integration fixture.",
        )

    def upsert_issue_comment(self, issue_number: int, body: str, *, marker: str) -> int:
        for index, current in enumerate(self.comments):
            if marker in current:
                self.comments[index] = body
                return index + 1
        self.comments.append(body)
        return len(self.comments)

    def get_branch_head_sha(self, branch: str) -> str | None:
        return self.branch_heads.get(branch)

    def find_open_pr_by_branch(self, branch: str, *, base: str | None = None):
        return None

    def create_draft_pr(self, *, title: str, body: str, head: str, base: str):
        self.pull_requests.append({"title": title, "body": body, "head": head, "base": base})
        return FakePullRequest()

    def mark_pull_request_ready(self, pr_number: int) -> None:
        self.ready_prs.append(pr_number)

    def get_latest_review_approval(self, pr_number: int, reviewer_login: str):
        return self.approval

    def merge_approved_pull_request(
        self, pr_number: int, *, reviewer_login: str,
        expected_head_sha: str, merge_method: str,
    ):
        self.merge_calls.append({
            "reviewer": reviewer_login,
            "head": expected_head_sha,
            "method": merge_method,
        })
        self.merged = True
        return dict(self.approval or {})

    def get_commit_ci(self, commit_sha: str):
        return self.ci_state, [{"sha": commit_sha}]

    def close_issue(self, issue_number: int):
        self.closed_issues.append(issue_number)
        return True

    def create_release_promotion_pr(self, *, head: str, base: str):
        self.release_prs.append({"head": head, "base": base})
        return SimpleNamespace(
            number=24,
            html_url="https://example.test/pull/24",
        )

    def get_pull_request_ci(self, pr_number: int):
        return self.ci_state, {"run_id": 9001, "url": "https://example.test/actions/9001"}

    def get_pull_request_details(self, pr_number: int):
        pull_request = self.pull_requests[-1]
        return {
            "number": pr_number,
            "url": "https://example.test/pull/23",
            "state": "open",
            "is_merged": self.merged,
            "is_draft": pr_number not in self.ready_prs,
            "base_ref": pull_request["base"],
            "head_ref": pull_request["head"],
            "head_sha": self.branch_heads.get(pull_request["head"], "final-sha"),
            "merge_commit_sha": "c" * 40 if self.merged else None,
            "merged_at": "2026-10-07T12:00:00Z" if self.merged else "",
            "merged_by": "investory-orchestrator[bot]" if self.merged else "",
            "title": pull_request["title"],
            "body": pull_request["body"],
        }


class OrchestrationIntegrationTests(unittest.TestCase):
    def test_ci_failure_blocks_and_terminal_notification_is_idempotent(self) -> None:
        github = FakeGitHub()
        github.ci_state = "failure"
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "DATABASE_URL": "",
                "GITHUB_REPOSITORY": "spider-su/investory",
                "BASE_BRANCH": "develop",
            }
            with patch.dict(os.environ, env, clear=False):
                store = TaskStore(Path(directory) / "tasks.db")
                task = store.create(
                    issue_number=77,
                    title="Failing CI fixture",
                    source="github_issue",
                    repository=env["GITHUB_REPOSITORY"],
                )
                for status in (
                    TaskStatus.PLANNING,
                    TaskStatus.IMPLEMENTING,
                    TaskStatus.PUBLISHING,
                    TaskStatus.WAITING_CI,
                ):
                    task = store.transition(task.task_id, status)
                task = store.transition(
                    task.task_id,
                    TaskStatus.WAITING_CI,
                    pr_number=31,
                    pr_url="https://example.test/pull/31",
                    ci_status="queued",
                )
                with patch("app.github_client.GitHubAppClient", return_value=github):
                    _poll_ci(store)
                    blocked = store.get(task.task_id)
                    self.assertEqual(blocked.status, TaskStatus.BLOCKED)
                    self.assertEqual(blocked.ci_status, "failed")
                    self.assertEqual(blocked.ci_attempts, 1)
                    self.assertIn("CI failed", blocked.blocked_reason)

                    _notify_terminal_tasks(store)
                    _notify_terminal_tasks(store)
                    blocked = store.get(task.task_id)

        terminal = [comment for comment in github.comments if "terminal-notification" in comment]
        self.assertEqual(len(terminal), 1)
        self.assertIn("@spider-su", terminal[0])
        self.assertIn("blocked and needs attention", terminal[0])
        self.assertEqual(blocked.metadata["terminal_notification_status"], "BLOCKED")

    def test_issue_approval_merges_develop_and_opens_release_pr(self) -> None:
        github = FakeGitHub()
        plan = ImplementationPlan(
            goal="Complete the fixture",
            summary="Add and verify a deterministic fixture.",
            assumptions=[],
            open_questions=[],
            acceptance_criteria=["The fixture is present."],
            steps=[PlanStep(
                id="step-01", title="Add fixture", goal="Add the fixture.",
                requirements=["Add the fixture."],
                acceptance_criteria=["The fixture is present."],
                validation=["Run validation."], affected_areas=["tests"],
                out_of_scope=[], depends_on=[],
            )],
        )
        approved = ReviewResult(status="approved", summary="All requirements pass.")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            env = {
                "DATABASE_URL": "",
                "TASK_DB": str(root / "tasks.db"),
                "CHECKPOINT_DB": str(root / "checkpoints.db"),
                "GITHUB_REPOSITORY": "spider-su/investory",
                "GITHUB_APP_ID": "test-app",
                "BASE_BRANCH": "develop",
                "PUBLISH_PLAN_COMMENT": "true",
                "PUBLISH_REVIEW_COMMENT": "true",
                "MAX_ATTEMPTS": "2",
                "CODER_MODEL": "codex-coder",
                "REVIEWER_MODEL": "codex-reviewer",
            }
            with patch.dict(os.environ, env, clear=False):
                store = TaskStore(env["TASK_DB"])
                task = store.create(
                    issue_number=42,
                    title="Integration fixture",
                    body="Implement the deterministic integration fixture.",
                    source="github_issue",
                    repository=env["GITHUB_REPOSITORY"],
                    metadata={"base_branch": "develop"},
                )
                with ExitStack() as stack:
                    stack.enter_context(patch("app.graph.GitHubAppClient", return_value=github))
                    stack.enter_context(patch("app.github_client.GitHubAppClient", return_value=github))
                    stack.enter_context(patch("app.graph.prepare_workspace", return_value=(workspace, "agent/issue-42")))
                    stack.enter_context(patch("app.graph.capture_workspace_audit", return_value={"branch": "agent/issue-42", "clean": True}))
                    stack.enter_context(patch("app.graph.collect_repository_context", return_value="fixture context"))
                    stack.enter_context(patch("app.graph.create_plan", return_value=plan))
                    stack.enter_context(patch("app.graph.start_environment", return_value={"success": True, "exit_code": 0, "output": "ready"}))
                    stack.enter_context(patch("app.graph.stop_environment", return_value={"success": True, "exit_code": 0, "output": "stopped"}))
                    validation = stack.enter_context(patch("app.graph.run_validation", return_value={"success": True, "exit_code": 0, "output": "validation passed"}))
                    coder = stack.enter_context(patch("app.graph.run_coder", return_value="Implemented fixture."))
                    stack.enter_context(patch("app.graph.coder_identity", return_value={"backend": "codex-cli", "provider": "codex-cli", "model": "codex-coder"}))
                    step_review = stack.enter_context(patch("app.graph.review_implementation", return_value=approved))
                    stack.enter_context(patch("app.graph.review_identity", return_value={"backend": "codex-cli", "provider": "codex-cli", "model": "codex-reviewer"}))
                    stack.enter_context(patch("app.graph.workspace_has_changes", return_value=False))
                    stack.enter_context(patch("app.graph.current_head", return_value="baseline-sha"))
                    stack.enter_context(patch("app.graph.commit_step", return_value="checkpoint-sha"))
                    stack.enter_context(patch("app.graph.finalize_checkpoint_history", return_value="final-sha"))
                    stack.enter_context(patch("app.graph.push_branch", side_effect=lambda *args, **kwargs: github.branch_heads.update({"agent/issue-42": "final-sha"})))
                    final_review = stack.enter_context(patch("app.agents.reviewer.review_implementation", return_value=approved))
                    stack.enter_context(patch("app.agents.reviewer.review_identity", return_value={"backend": "codex-cli", "provider": "codex-cli", "model": "codex-reviewer"}))
                    def git_output(command, **kwargs):
                        if command[:2] == ["git", "rev-parse"]:
                            return SimpleNamespace(stdout="final-sha")
                        if command[:2] == ["git", "branch"]:
                            return SimpleNamespace(stdout="agent/issue-42")
                        return SimpleNamespace(stdout="")

                    stack.enter_context(patch(
                        "app.task_scheduler.subprocess.run",
                        side_effect=git_output,
                    ))
                    run_cli(
                        build_graph=build_graph,
                        resolve_resume_from=resolve_resume_from,
                        reload_issue_for_planning=reload_issue_for_planning,
                        argv=["--task-id", task.task_id, "--issue", "42"],
                    )
                    waiting = store.get(task.task_id)
                    self.assertEqual(waiting.status, TaskStatus.WAITING_CI)
                    self.assertEqual(waiting.pr_number, 23)
                    self.assertEqual(waiting.pr_url, "https://example.test/pull/23")

                    _poll_ci(store)
                    ready = store.get(task.task_id)
                    self.assertEqual(ready.status, TaskStatus.READY)
                    self.assertEqual(ready.ci_status, "green")
                    self.assertEqual(ready.metadata["final_review_independence"], "independent")
                    self.assertTrue(all(ready.metadata["ready_gates"].values()))

                    _notify_terminal_tasks(store)
                    ready_notified = store.get(task.task_id)
                    ready_notification = next(
                        comment for comment in github.comments
                        if "terminal-notification" in comment
                    )

                    github.approval = {
                        "reviewer": "spider-su",
                        "state": "APPROVED",
                        "commit_sha": "final-sha",
                        "current_head_sha": "final-sha",
                        "review_id": "7001",
                        "submitted_at": "2026-10-07T12:00:00Z",
                    }
                    self.assertEqual(
                        ready.metadata.get("final_review_head_sha"), "final-sha"
                    )
                    _poll_ci(store)
                    completed = store.get(task.task_id)
                    store.transition(
                        task.task_id,
                        TaskStatus.COMPLETED,
                        metadata={
                            **completed.metadata,
                            "release_promotion": {
                                **completed.metadata["release_promotion"],
                                "status": "pending",
                            },
                        },
                    )
                    _notify_terminal_tasks(store)
                    pending_notification = next(
                        comment for comment in github.comments
                        if "terminal-notification" in comment
                    )
                    pending = store.get(task.task_id)
                    store.transition(
                        task.task_id,
                        TaskStatus.COMPLETED,
                        metadata={
                            **pending.metadata,
                            "release_promotion": {
                                **pending.metadata["release_promotion"],
                                "status": "awaiting_review",
                            },
                        },
                    )
                    _notify_terminal_tasks(store)

        self.assertEqual(ready_notified.status, TaskStatus.READY)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.metadata["completion"]["source"], "approved_review")
        self.assertEqual(completed.metadata["completion"]["approval_review"]["review_id"], "7001")
        self.assertTrue(completed.metadata["completion"]["issue_closed"])
        self.assertEqual(
            completed.metadata["release_promotion"]["status"], "awaiting_review"
        )
        self.assertEqual(completed.metadata["release_promotion"]["pull_request_number"], 24)
        self.assertEqual(github.merge_calls[0]["reviewer"], "spider-su")
        self.assertEqual(github.merge_calls[0]["head"], "final-sha")
        self.assertEqual(github.release_prs, [{"head": "develop", "base": "main"}])
        self.assertEqual(github.closed_issues, [42])
        terminal_comment = next(
            comment for comment in github.comments
            if "terminal-notification" in comment
        )
        self.assertIn("@spider-su", terminal_comment)
        self.assertIn("Merged PR: https://example.test/pull/23", terminal_comment)
        self.assertNotIn("Draft PR", terminal_comment)
        self.assertIn("Release promotion PR: https://example.test/pull/24", terminal_comment)
        self.assertIn("Preparing the development-to-release promotion PR.", pending_notification)
        self.assertIn("approving GitHub review", ready_notification)
        self.assertEqual(github.ready_prs, [23])
        self.assertEqual(len(github.pull_requests), 1)
        self.assertEqual(github.pull_requests[0]["base"], "develop")
        self.assertEqual(coder.call_count, 1)
        self.assertEqual(validation.call_count, 2)
        self.assertEqual(step_review.call_count, 2)
        self.assertEqual(final_review.call_count, 1)


if __name__ == "__main__":
    unittest.main()
