from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.planner import ImplementationPlan, PlanStep
from app.graph import finalize_history_node, planner_node, route_after_validation
from app.retry_isolation import archive_and_reset_failed_attempt
from app.side_effects import prepare_finalization_intent
from app.workspace import commit_step, finalize_checkpoint_history


def git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=workspace, text=True).strip()


class IncrementalWorkflowTests(unittest.TestCase):
    def test_simplified_plan_preserves_testable_steps(self) -> None:
        steps = [PlanStep(
            id=f"step-0{index}", title=f"Increment {index}", goal="Tested increment",
            requirements=[f"Requirement {index}"], acceptance_criteria=["Tests green"],
            validation=["unit tests"], affected_areas=["service.txt"],
            out_of_scope=[], depends_on=[] if index == 1 else ["step-01"],
        ) for index in (1, 2)]
        plan = ImplementationPlan(goal="Complete request", summary="Two increments",
            assumptions=[], open_questions=[], acceptance_criteria=["All complete"],
            steps=steps)
        with patch("app.graph.create_plan", return_value=plan):
            result = planner_node({"workflow_mode": "simplified", "issue_number": 1,
                "issue_title": "Incremental", "issue_body": "Implement", "workspace": "/tmp",
                "repository_context": ""})
        self.assertEqual([step.model_dump() for step in steps], result["plan"]["steps"])
        self.assertEqual(2, len(result["steps"]))
        self.assertEqual("prepare_checkpoint", route_after_validation({
            "workflow_mode": "simplified", "validation_status": "validation_success"}))

    def test_graph_selects_preserved_history_for_simplified_mode(self) -> None:
        intent = prepare_finalization_intent(issue_number=1, baseline_sha="a", checkpoint_sha="b")
        with patch("app.graph.finalize_checkpoint_history", return_value="b") as finalize:
            result = finalize_history_node({"workflow_mode": "simplified", "workspace": "/tmp",
                "issue_number": 1, "issue_title": "Incremental", "steps": [],
                "side_effect_intent": intent})
        self.assertTrue(finalize.call_args.kwargs["preserve_step_commits"])
        self.assertEqual("b", result["final_commit_sha"])

    def test_failed_later_step_and_final_repair_preserve_successful_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            git(workspace, "init", "-q")
            git(workspace, "config", "user.name", "Test")
            git(workspace, "config", "user.email", "test@example.com")
            source = workspace / "service.txt"
            source.write_text("baseline\n")
            git(workspace, "add", ".")
            git(workspace, "commit", "-qm", "Baseline")
            baseline = git(workspace, "rev-parse", "HEAD")
            source.write_text("step one\n")
            first = commit_step(workspace, "step-01", "First increment")
            source.write_text("failed second step\n")
            # Put artifacts outside the checkout: retry cleans untracked files.
            with tempfile.TemporaryDirectory() as artifacts:
                with patch.dict("os.environ", {"RUNS_DIR": artifacts}):
                    archive = archive_and_reset_failed_attempt(workspace=workspace,
                        issue_number=1, step_id="step-02", attempt=1,
                        failure_stage="validation", baseline_sha=first,
                        coder_summary="Second increment", validation_output="Failed test",
                        validation_exit_code=1, review={})
                    self.assertIn("failed second step", Path(archive["patch_path"]).read_text())
            self.assertEqual(first, git(workspace, "rev-parse", "HEAD"))
            self.assertEqual("step one\n", source.read_text())
            source.write_text("step two\n")
            second = commit_step(workspace, "step-02", "Second increment", expected_parent_sha=first)
            kwargs = dict(baseline_sha=baseline, expected_checkpoint_sha=second,
                operation_id="incremental-finalization", issue_number=1,
                issue_title="Incremental", allowed_paths=["service.txt"], preserve_step_commits=True)
            self.assertEqual(second, finalize_checkpoint_history(workspace, **kwargs))
            self.assertEqual(second, finalize_checkpoint_history(workspace, **kwargs))
            source.write_text("reviewed integration repair\n")
            final = finalize_checkpoint_history(workspace, **kwargs)
            self.assertEqual(second, git(workspace, "rev-parse", f"{final}^"))
            self.assertEqual(first, git(workspace, "rev-parse", f"{second}^"))
            self.assertEqual(final, finalize_checkpoint_history(workspace, **kwargs))
            self.assertEqual("", git(workspace, "status", "--porcelain"))

    def test_preserved_history_refuses_changed_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            git(workspace, "init", "-q")
            git(workspace, "config", "user.name", "Test")
            git(workspace, "config", "user.email", "test@example.com")
            git(workspace, "commit", "--allow-empty", "-qm", "Baseline")
            baseline = git(workspace, "rev-parse", "HEAD")
            with self.assertRaisesRegex(RuntimeError, "HEAD changed"):
                finalize_checkpoint_history(workspace, baseline_sha=baseline,
                    expected_checkpoint_sha="unexpected", operation_id="finalize",
                    issue_number=1, issue_title="Test", preserve_step_commits=True)
