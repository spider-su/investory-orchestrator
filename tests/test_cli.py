from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.cli import build_initial_state, run_cli


class FakeGraph:
    def __init__(self, snapshot_values: dict | None = None) -> None:
        self.snapshot_values = snapshot_values or {}
        self.invocations: list[tuple[object, dict]] = []
        self.updates: list[tuple[dict, dict, str]] = []

    def get_state(self, config: dict):
        return SimpleNamespace(values=self.snapshot_values)

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

    def test_build_initial_state_has_remote_operation_defaults(self) -> None:
        state = build_initial_state(7)

        self.assertEqual(state["side_effect_intent"], {})
        self.assertEqual(state["side_effect_history"], [])
        self.assertEqual(state["workflow_status"], "new")


if __name__ == "__main__":
    unittest.main()
