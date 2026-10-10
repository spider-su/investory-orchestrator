from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.reviewer import ReviewResult, review_implementation
from app.graph import final_reviewer_node, finalize_history_node
from app.side_effects import prepare_finalization_intent
from app.task_scheduler import _reuse_published_review, _sync_task_result
from app.tasks import TaskStore, TaskStatus
from app.workspace import candidate_tree_sha


def git(workspace, *args):
    return subprocess.check_output(["git", *args], cwd=workspace, text=True).strip()


class BatchedReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name) / "checkout"
        self.workspace.mkdir()
        git(self.workspace, "init", "-q")
        git(self.workspace, "config", "user.name", "Test")
        git(self.workspace, "config", "user.email", "test@example.com")
        (self.workspace / "code.txt").write_text("baseline\n")
        git(self.workspace, "add", ".")
        git(self.workspace, "commit", "-qm", "Baseline")
        self.store = TaskStore(Path(self.directory.name) / "tasks.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_candidate_tree_captures_changes_without_altering_index(self):
        (self.workspace / "code.txt").write_text("staged\n")
        git(self.workspace, "add", "code.txt")
        index_before = git(self.workspace, "write-tree")
        (self.workspace / "code.txt").write_text("reviewed\n")
        (self.workspace / "new.txt").write_text("new file\n")
        tree = candidate_tree_sha(self.workspace)
        self.assertEqual(index_before, git(self.workspace, "write-tree"))
        self.assertEqual("reviewed", git(self.workspace, "show", tree + ":code.txt"))
        self.assertEqual("new file", git(self.workspace, "show", tree + ":new.txt"))
        git(self.workspace, "add", "-A")
        git(self.workspace, "commit", "-qm", "Publish")
        self.assertEqual(tree, git(self.workspace, "rev-parse", "HEAD^{tree}"))

    def test_repair_reviews_delta_and_preserves_todos(self):
        (self.workspace / "code.txt").write_text("first candidate\n")
        tree = candidate_tree_sha(self.workspace)
        (self.workspace / "code.txt").write_text("repaired candidate\n")
        previous = {"status": "changes_required", "findings": [{
            "severity": "warning", "title": "Optional polish",
            "description": "Can be done later.", "recommendation": "Follow up.",
        }]}
        with patch("app.agents.reviewer.run_structured_prompt", return_value=ReviewResult(
            status="approved", summary="Repair verified.",
        )) as run:
            result = review_implementation(
                workspace=self.workspace, issue_number=1, issue_title="Feature",
                issue_body="Implement feature", plan={}, validation_output="passed " * 20000,
                review_scope="whole_plan", baseline_sha=git(self.workspace, "rev-parse", "HEAD"),
                previous_review=previous, previous_review_tree_sha=tree,
            )
        prompt = run.call_args.kwargs["prompt"]
        self.assertIn("-first candidate", prompt)
        self.assertIn("+repaired candidate", prompt)
        self.assertNotIn("-baseline", prompt)
        self.assertLess(len(prompt), 25000)
        self.assertEqual("approved", result.status)
        self.assertEqual("Optional polish", result.findings[0].title)

    def test_changes_during_review_invalidate_approval(self):
        def mutate(**kwargs):
            (self.workspace / "code.txt").write_text("changed during review\n")
            return ReviewResult(status="approved", summary="Approved")
        state = {"workspace": str(self.workspace), "issue_number": 1,
                 "issue_title": "Feature", "issue_body": "Feature", "plan": {},
                 "final_validation_output": "passed", "issue_baseline_sha": git(self.workspace, "rev-parse", "HEAD")}
        with patch("app.graph.review_implementation", side_effect=mutate):
            result = final_reviewer_node(state)
        self.assertEqual("review_failure", result["final_review_status"])
        self.assertIn("changed during", result["error"])

    def test_finalization_refuses_files_changed_after_review(self):
        head = git(self.workspace, "rev-parse", "HEAD")
        tree = candidate_tree_sha(self.workspace)
        (self.workspace / "code.txt").write_text("unreviewed\n")
        state = {"workspace": str(self.workspace), "workflow_mode": "simplified",
                 "issue_number": 1, "issue_title": "Feature", "steps": [],
                 "final_review_tree_sha": tree,
                 "side_effect_intent": prepare_finalization_intent(issue_number=1, baseline_sha=head, checkpoint_sha=head)}
        with patch("app.graph.finalize_checkpoint_history", return_value=head):
            result = finalize_history_node(state)
        self.assertEqual("blocked", result["workflow_status"])

    def published_task(self):
        head = "a" * 40
        task = self.store.create(title="Feature")
        self.store.transition(task.task_id, TaskStatus.RUNNING)
        _sync_task_result(self.store, task.task_id, {
            "workflow_status": "completed", "pull_request_number": 1,
            "pull_request_url": "https://example.test/pr/1", "branch": "agent/issue-1",
            "workspace": str(self.workspace), "final_commit_sha": head,
            "final_review_status": "approved", "final_review": {"status": "approved", "summary": "Approved"},
            "final_review_head_sha": head, "final_review_tree_sha": "b" * 40,
            "final_validation_tree_sha": "b" * 40,
            "final_review_clean_worktree": True, "final_validation_status": "validation_success",
            "coder_model": "coder", "coder_provider": "codex-cli",
            "reviewer_model": "reviewer", "reviewer_provider": "codex-cli", "reviewer_backend": "codex-cli",
        })
        return self.store.get(task.task_id)

    def test_green_ci_reuses_approval_for_same_commit_without_codex(self):
        task = self.published_task()
        client = SimpleNamespace(get_commit_ci=lambda sha: ("success", []))
        with patch("app.agents.reviewer.run_structured_prompt") as codex:
            self.assertTrue(_reuse_published_review(self.store, task, client, "a" * 40))
        codex.assert_not_called()
        ready = self.store.get(task.task_id)
        self.assertEqual(TaskStatus.READY, ready.status)
        self.assertTrue(all(ready.metadata["ready_gates"].values()))

    def test_changed_head_or_missing_evidence_cannot_reuse_approval(self):
        task = self.published_task()
        client = SimpleNamespace(get_commit_ci=lambda sha: self.fail("CI should not be requested"))
        self.assertFalse(_reuse_published_review(self.store, task, client, "c" * 40))
        for field, value in [("final_review_tree_sha", ""), ("final_review_clean_worktree", False),
                             ("final_validation_status", "failed"), ("coder_model", "reviewer"),
                             ("final_validation_tree_sha", "c" * 40)]:
            metadata = {**task.metadata, field: value}
            modified = self.store.transition(task.task_id, task.status, metadata=metadata)
            self.assertFalse(_reuse_published_review(self.store, modified, client, "a" * 40))

    def test_pending_or_red_ci_cannot_make_task_ready(self):
        for state, expected in [("pending", TaskStatus.WAITING_CI), ("failure", TaskStatus.BLOCKED)]:
            task = self.published_task()
            client = SimpleNamespace(get_commit_ci=lambda sha: (state, []))
            self.assertTrue(_reuse_published_review(self.store, task, client, "a" * 40))
            self.assertEqual(expected, self.store.get(task.task_id).status)


if __name__ == "__main__":
    unittest.main()
