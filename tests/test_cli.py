from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.cli import build_initial_state, run_cli
from app.tasks import TaskStatus, TaskStore


class FakeGraph:
    def __init__(
        self,
        snapshot_values: dict | None = None,
        snapshot_next: tuple[str, ...] = (),
    ) -> None:
        self.snapshot_values = snapshot_values or {}
        self.snapshot_next = snapshot_next
        self.invocations: list[tuple[object, dict]] = []
        self.updates: list[tuple[dict, dict, str]] = []

    def get_state(self, config: dict):
        return SimpleNamespace(values=self.snapshot_values, next=self.snapshot_next)

    def update_state(
        self,
        config: dict,
        updates: dict,
        *,
        as_node: str,
    ) -> None:
        self.updates.append((config, updates, as_node))

    def invoke(self, state, *, config: dict):
        self.invocations.append((state, config))
        return state

    def stream(self, state, *, config: dict, stream_mode: str):
        return iter(())


class CliTests(unittest.TestCase):
    def test_new_run_builds_initial_state_and_stable_thread(self) -> None:
        graph = FakeGraph()

        with patch.dict(
            os.environ,
            {
                "MAX_ATTEMPTS": "4",
                "MAX_FINAL_ATTEMPTS": "5",
            },
        ):
            run_cli(
                build_graph=lambda: graph,
                resolve_resume_from=Mock(),
                reload_issue_for_planning=Mock(),
                argv=["--issue", "42"],
            )

        self.assertEqual(len(graph.invocations), 1)
        state, config = graph.invocations[0]
        self.assertEqual(state["issue_number"], 42)
        self.assertEqual(state["max_attempts"], 4)
        self.assertEqual(state["max_final_attempts"], 5)
        self.assertEqual(
            config["configurable"]["thread_id"],
            "investory-issue-42",
        )

    def test_resume_updates_checkpoint_then_continues(self) -> None:
        saved_state = {
            "workflow_status": "blocked",
            "blocked_stage": "reviewer",
            "max_attempts": 3,
            "max_final_attempts": 3,
            "attempt": 1,
            "final_attempt": 0,
        }
        graph = FakeGraph(saved_state)
        resolver = Mock(return_value="run_validation")
        reloader = Mock()

        run_cli(
            build_graph=lambda: graph,
            resolve_resume_from=resolver,
            reload_issue_for_planning=reloader,
            argv=["--issue", "42", "--resume"],
        )

        resolver.assert_called_once_with(saved_state)
        reloader.assert_not_called()
        self.assertEqual(len(graph.updates), 1)

        config, updates, as_node = graph.updates[0]
        self.assertEqual(
            config["configurable"]["thread_id"],
            "investory-issue-42",
        )
        self.assertEqual(as_node, "run_validation")
        self.assertEqual(updates["workflow_status"], "implementing")
        self.assertEqual(updates["blocked_stage"], "")
        self.assertEqual(graph.invocations, [(None, config)])

    def test_coder_resume_keeps_consumed_attempt(self) -> None:
        saved_state = {
            "workflow_status": "blocked",
            "blocked_stage": "coder",
            "max_attempts": 3,
            "max_final_attempts": 3,
            "attempt": 2,
            "final_attempt": 0,
        }
        graph = FakeGraph(saved_state)
        resolver = Mock(return_value="prepare_current_step")

        run_cli(
            build_graph=lambda: graph,
            resolve_resume_from=resolver,
            reload_issue_for_planning=Mock(),
            argv=["--issue", "42", "--resume"],
        )

        self.assertEqual(graph.updates[0][2], "prepare_current_step")
        self.assertNotIn("attempt", graph.updates[0][1])
        self.assertEqual(saved_state["attempt"], 2)

    def test_resume_with_pending_coder_node_checks_workspace_before_invoking(self) -> None:
        saved_state = {
            "workflow_status": "reviewing",
            "workspace": "/path/that/does/not/exist",
            "max_attempts": 3,
            "max_final_attempts": 3,
            "attempt": 1,
            "final_attempt": 0,
        }
        graph = FakeGraph(saved_state, snapshot_next=("coder",))

        run_cli(
            build_graph=lambda: graph,
            resolve_resume_from=Mock(),
            reload_issue_for_planning=Mock(),
            argv=["--issue", "42", "--resume"],
        )

        self.assertEqual(len(graph.invocations), 1)

    def test_build_initial_state_has_remote_operation_defaults(self) -> None:
        state = build_initial_state(7)

        self.assertEqual(state["side_effect_intent"], {})
        self.assertEqual(state["side_effect_history"], [])
        self.assertEqual(state["workflow_status"], "new")
        self.assertIn(state["workflow_mode"], {"legacy", "simplified"})

    def test_build_initial_state_rejects_unknown_workflow_mode(self) -> None:
        with patch.dict("os.environ", {"WORKFLOW_MODE": "unknown"}):
            with self.assertRaisesRegex(ValueError, "WORKFLOW_MODE"):
                build_initial_state(7)

    def test_ci_repair_routes_saved_feedback_to_final_integration_coder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "tasks.db")
            store = TaskStore(database)
            task = store.create(title="Repair CI")
            task = store.transition(task.task_id, TaskStatus.BLOCKED)
            store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                ci_status="failed",
                pr_number=8,
                pr_url="https://example.test/pr/8",
                metadata={
                    "ci_details": [{
                        "name": "tests",
                        "conclusion": "failure",
                        "url": "https://example.test/check/1",
                        "output": "assertion failed",
                    }]
                },
            )
            graph = FakeGraph({"workflow_status": "blocked"})

            with patch.dict(os.environ, {"TASK_DB": database}):
                run_cli(
                    build_graph=lambda: graph,
                    resolve_resume_from=Mock(),
                    reload_issue_for_planning=Mock(),
                    argv=["--task-id", task.task_id, "--resume", "--ci-repair"],
                )

        _config, updates, as_node = graph.updates[0]
        self.assertEqual(as_node, "prepare_final_review")
        self.assertTrue(updates["ci_repair_requested"])
        self.assertIn("assertion failed", updates["final_validation_output"])


if __name__ == "__main__":
    unittest.main()
