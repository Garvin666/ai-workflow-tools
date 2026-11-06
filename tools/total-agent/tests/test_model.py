"""数据模型与状态机测试。重点：非法迁移必须抛错，不能静默放过。"""

from __future__ import annotations

import unittest

from total_agent.errors import IllegalTransition, PlanError
from total_agent.model import (
    RunState,
    RunStatus,
    Step,
    StepState,
    StepStatus,
    TaskSpec,
    transition,
    transition_run,
)


class TestStepTransition(unittest.TestCase):
    def test_happy_path(self) -> None:
        st = StepState(id="a")
        transition(st, StepStatus.READY)
        transition(st, StepStatus.RUNNING)
        transition(st, StepStatus.SUCCEEDED)
        self.assertIs(st.status, StepStatus.SUCCEEDED)

    def test_pending_cannot_jump_to_succeeded(self) -> None:
        st = StepState(id="a")
        with self.assertRaises(IllegalTransition):
            transition(st, StepStatus.SUCCEEDED)

    def test_terminal_is_stuck(self) -> None:
        for terminal in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
            st = StepState(id="a", status=terminal)
            with self.assertRaises(IllegalTransition):
                transition(st, StepStatus.RUNNING)

    def test_retry_running_back_to_ready(self) -> None:
        st = StepState(id="a", status=StepStatus.RUNNING)
        transition(st, StepStatus.READY)
        self.assertIs(st.status, StepStatus.READY)

    def test_running_can_be_blocked(self) -> None:
        st = StepState(id="a", status=StepStatus.RUNNING)
        transition(st, StepStatus.BLOCKED)
        self.assertIs(st.status, StepStatus.BLOCKED)

    def test_self_transition_is_noop(self) -> None:
        st = StepState(id="a", status=StepStatus.READY)
        transition(st, StepStatus.READY)
        self.assertIs(st.status, StepStatus.READY)


class TestRunTransition(unittest.TestCase):
    def test_pause_and_resume(self) -> None:
        state = RunState(run_id="r", goal="g", steps={"a": StepState(id="a")})
        transition_run(state, RunStatus.PAUSED, "waiting")
        self.assertEqual(state.pause_reason, "waiting")
        transition_run(state, RunStatus.RUNNING)
        transition_run(state, RunStatus.SUCCEEDED)
        self.assertTrue(state.ended_at)

    def test_terminal_run_is_stuck(self) -> None:
        state = RunState(run_id="r", goal="g", status=RunStatus.SUCCEEDED)
        with self.assertRaises(IllegalTransition):
            transition_run(state, RunStatus.RUNNING)


class TestSpecValidation(unittest.TestCase):
    def test_missing_goal(self) -> None:
        with self.assertRaises(PlanError):
            TaskSpec.from_dict({"steps": [{"id": "a", "command": "x"}]})

    def test_empty_steps(self) -> None:
        with self.assertRaises(PlanError):
            TaskSpec.from_dict({"goal": "g", "steps": []})

    def test_shell_without_command(self) -> None:
        with self.assertRaises(PlanError):
            Step.from_dict({"id": "a", "executor": "shell"})

    def test_python_without_target(self) -> None:
        with self.assertRaises(PlanError):
            Step.from_dict({"id": "a", "executor": "python"})

    def test_bad_executor(self) -> None:
        with self.assertRaises(PlanError):
            Step.from_dict({"id": "a", "executor": "quantum", "command": "x"})

    def test_default_idempotency_key(self) -> None:
        s = Step.from_dict({"id": "a", "command": "echo", "executor": "shell"})
        self.assertEqual(s.key, "step:a")

    def test_explicit_idempotency_key_kept(self) -> None:
        s = Step.from_dict(
            {"id": "a", "command": "echo", "executor": "shell", "idempotency_key": "k1"}
        )
        self.assertEqual(s.key, "k1")


class TestSerialization(unittest.TestCase):
    def test_spec_roundtrip(self) -> None:
        raw = {
            "goal": "g",
            "steps": [
                {"id": "a", "executor": "shell", "command": "echo 1"},
                {"id": "b", "executor": "shell", "command": "echo 2", "depends_on": ["a"]},
            ],
            "boundaries": {"max_steps": 5},
        }
        spec = TaskSpec.from_dict(raw)
        again = TaskSpec.from_dict(spec.to_dict())
        self.assertEqual(again.goal, "g")
        self.assertEqual([s.id for s in again.steps], ["a", "b"])
        self.assertEqual(again.steps[1].depends_on, ("a",))
        self.assertEqual(again.boundaries, {"max_steps": 5})

    def test_state_roundtrip(self) -> None:
        state = RunState.create(
            TaskSpec.from_dict(
                {"goal": "g", "steps": [{"id": "a", "executor": "shell", "command": "echo"}]}
            )
        )
        state.steps["a"].status = StepStatus.SUCCEEDED
        state.steps["a"].attempts = 2
        again = RunState.from_dict(state.to_dict())
        self.assertIs(again.steps["a"].status, StepStatus.SUCCEEDED)
        self.assertEqual(again.steps["a"].attempts, 2)
        self.assertEqual(again.counters["steps_executed"], 0)

    def test_all_terminal(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "goal": "g",
                "steps": [
                    {"id": "a", "executor": "shell", "command": "echo"},
                    {"id": "b", "executor": "shell", "command": "echo"},
                ],
            }
        )
        state = RunState.create(spec)
        self.assertFalse(state.all_terminal())
        state.steps["a"].status = StepStatus.SUCCEEDED
        state.steps["b"].status = StepStatus.SKIPPED
        self.assertTrue(state.all_terminal())


if __name__ == "__main__":
    unittest.main()
