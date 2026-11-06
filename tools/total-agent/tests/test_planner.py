"""DAG 编排测试。重点：有环必须报错，优先级必须在依赖允许的范围内生效。"""

from __future__ import annotations

import unittest

from total_agent import planner
from total_agent.errors import PlanError
from total_agent.model import Step, StepState, StepStatus


def S(sid: str, deps=(), prio: int = 0) -> Step:
    return Step.from_dict(
        {"id": sid, "executor": "shell", "command": "echo", "depends_on": list(deps), "priority": prio}
    )


class TestValidation(unittest.TestCase):
    def test_duplicate_id(self) -> None:
        with self.assertRaises(PlanError):
            planner.validate([S("a"), S("a")])

    def test_unknown_dependency(self) -> None:
        with self.assertRaises(PlanError):
            planner.validate([S("a", deps=["ghost"])])

    def test_self_dependency(self) -> None:
        with self.assertRaises(PlanError):
            planner.validate([S("a", deps=["a"])])

    def test_cycle_detected(self) -> None:
        with self.assertRaises(PlanError) as ctx:
            planner.validate([S("a", deps=["c"]), S("b", deps=["a"]), S("c", deps=["b"])])
        self.assertIn("循环依赖", str(ctx.exception))

    def test_empty_plan(self) -> None:
        with self.assertRaises(PlanError):
            planner.validate([])


class TestTopoOrder(unittest.TestCase):
    def test_linear(self) -> None:
        steps = [S("c", deps=["b"]), S("a"), S("b", deps=["a"])]
        self.assertEqual(planner.topo_order(steps), ["a", "b", "c"])

    def test_diamond(self) -> None:
        steps = [S("d", deps=["b", "c"]), S("b", deps=["a"]), S("c", deps=["a"]), S("a")]
        ordered = planner.topo_order(steps)
        self.assertLess(ordered.index("a"), ordered.index("b"))
        self.assertLess(ordered.index("a"), ordered.index("c"))
        self.assertLess(ordered.index("b"), ordered.index("d"))
        self.assertLess(ordered.index("c"), ordered.index("d"))

    def test_stable_within_layer(self) -> None:
        steps = [S("x"), S("y"), S("z")]
        self.assertEqual(planner.topo_order(steps), ["x", "y", "z"])


class TestReadiness(unittest.TestCase):
    def _states(self, steps, overrides=None):
        states = {s.id: StepState(id=s.id) for s in steps}
        for sid, st in (overrides or {}).items():
            states[sid].status = st
        return states

    def test_root_ready(self) -> None:
        steps = [S("a"), S("b", deps=["a"])]
        states = self._states(steps)
        self.assertEqual(planner.ready_ids(steps, states), ["a"])

    def test_child_not_ready_until_parent_succeeds(self) -> None:
        steps = [S("a"), S("b", deps=["a"])]
        states = self._states(steps, {"a": StepStatus.RUNNING})
        self.assertEqual(planner.ready_ids(steps, states), [])

    def test_child_ready_after_parent_succeeds(self) -> None:
        steps = [S("a"), S("b", deps=["a"])]
        states = self._states(steps, {"a": StepStatus.SUCCEEDED})
        self.assertEqual(planner.ready_ids(steps, states), ["b"])

    def test_failed_parent_blocks_child(self) -> None:
        steps = [S("a"), S("b", deps=["a"])]
        states = self._states(steps, {"a": StepStatus.FAILED})
        self.assertEqual(planner.ready_ids(steps, states), [])

    def test_ready_step_still_eligible(self) -> None:
        """重试把步骤放回 ready，它必须还能被选中。"""
        steps = [S("a")]
        states = self._states(steps, {"a": StepStatus.READY})
        self.assertEqual(planner.ready_ids(steps, states), ["a"])


class TestPriority(unittest.TestCase):
    def test_priority_wins_within_ready_set(self) -> None:
        steps = [S("low", prio=1), S("high", prio=9), S("mid", prio=5)]
        states = {s.id: StepState(id=s.id) for s in steps}
        self.assertEqual(planner.pick_next(steps, states).id, "high")

    def test_dependency_overrides_priority(self) -> None:
        """高优先级的下游不能越过未完成的依赖。"""
        steps = [S("base", prio=0), S("urgent", deps=["base"], prio=100)]
        states = {s.id: StepState(id=s.id) for s in steps}
        self.assertEqual(planner.pick_next(steps, states).id, "base")

    def test_none_when_nothing_ready(self) -> None:
        steps = [S("a")]
        states = {"a": StepState(id="a", status=StepStatus.SUCCEEDED)}
        self.assertIsNone(planner.pick_next(steps, states))


class TestDownstream(unittest.TestCase):
    def test_transitive(self) -> None:
        steps = [S("a"), S("b", deps=["a"]), S("c", deps=["b"]), S("x")]
        self.assertEqual(planner.downstream(steps, "a"), ["b", "c"])
        self.assertEqual(planner.downstream(steps, "x"), [])
        self.assertEqual(planner.downstream(steps, "c"), [])

    def test_blocked_by_unsatisfiable(self) -> None:
        steps = [S("a"), S("b", deps=["a"]), S("c", deps=["b"])]
        states = {s.id: StepState(id=s.id) for s in steps}
        states["a"].status = StepStatus.FAILED
        self.assertEqual(planner.blocked_by_unsatisfiable(steps, states), ["b"])
        states["b"].status = StepStatus.SKIPPED
        self.assertEqual(planner.blocked_by_unsatisfiable(steps, states), ["c"])


if __name__ == "__main__":
    unittest.main()
