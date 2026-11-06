"""运行时主循环测试。重点：成功/重试/失败传播/越界/预算/恢复六条路径各跑一遍。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from total_agent.errors import StateError
from total_agent.model import RunState, RunStatus, StepStatus
from total_agent.policy import Boundaries, Policy
from total_agent.runtime import Runtime
from total_agent.store import RunStore

from _helpers import counter_target, failing_target, make_spec, py_cmd


def P(sid, target, deps=(), **kw):
    raw = {"id": sid, "executor": "python", "target": target, "depends_on": list(deps)}
    raw.update(kw)
    return raw


class RuntimeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, steps, targets=None, boundaries=None, dry_run=False):
        b = boundaries or {}
        spec = make_spec(steps, boundaries=b)
        policy = Policy(Boundaries.from_dict({**b, "dry_run": dry_run}, self.root))
        store = RunStore(self.root / "run")
        rt = Runtime(spec, store, policy, python_registry=targets or {}, dry_run=dry_run)
        return rt, store, spec

    def reopen(self, store: RunStore, targets=None) -> Runtime:
        return Runtime(
            store.load_spec(),
            store,
            Policy(Boundaries(workspace_root=self.root)),
            python_registry=targets or {},
        )


class TestHappyPath(RuntimeTestBase):
    def test_two_steps_succeed_in_order(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("a", "hit", priority=1), P("b", "hit", deps=["a"])],
            {"hit": counter_target(calls)},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(calls, ["hit:a", "hit:b"])
        self.assertIs(state.steps["a"].status, StepStatus.SUCCEEDED)
        self.assertTrue(store.state_path.exists())

    def test_events_recorded(self) -> None:
        rt, store, spec = self.build([P("a", "hit")], {"hit": counter_target([])})
        rt.run()
        events = [e["event"] for e in store.read_events()]
        for expected in ("run_started", "step_started", "step_finished", "run_finished"):
            self.assertIn(expected, events)

    def test_no_verification_signal_is_marked_none(self) -> None:
        rt, store, spec = self.build([P("a", "hit")], {"hit": counter_target([])})
        state = rt.run()
        self.assertEqual(state.steps["a"].evidence["verification"], "none")

    def test_priority_ordering(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("low", "hit", priority=1), P("high", "hit", priority=9)],
            {"hit": counter_target(calls)},
        )
        rt.run()
        self.assertEqual(calls, ["hit:high", "hit:low"])


class TestRetry(RuntimeTestBase):
    def test_retry_then_success(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("a", "flaky", max_attempts=3)], {"flaky": failing_target(calls, times=2)}
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.steps["a"].attempts, 3)
        self.assertEqual(len(state.steps["a"].retry_notes), 2)

    def test_retry_exhausted_marks_failed(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("a", "flaky", max_attempts=2)], {"flaky": failing_target(calls, times=99)}
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.FAILED)
        self.assertIs(state.steps["a"].status, StepStatus.FAILED)
        self.assertEqual(state.steps["a"].attempts, 2)
        self.assertEqual(state.steps["a"].error["code"], "nonzero_exit")

    def test_retry_events_logged(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("a", "flaky", max_attempts=2)], {"flaky": failing_target(calls, times=1)}
        )
        rt.run()
        retries = [e for e in store.read_events() if e["event"] == "retry"]
        self.assertEqual(len(retries), 1)
        self.assertIn("第 2 次", retries[0]["note"])


class TestFailurePropagation(RuntimeTestBase):
    def test_downstream_skipped(self) -> None:
        seen: list[str] = []
        rt, store, spec = self.build(
            [P("a", "noop"), P("b", "boom", deps=["a"]), P("c", "noop", deps=["b"])],
            {"noop": counter_target([]), "boom": failing_target(seen, times=99)},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.FAILED)
        self.assertIs(state.steps["b"].status, StepStatus.FAILED)
        self.assertIs(state.steps["c"].status, StepStatus.SKIPPED)
        self.assertEqual(state.steps["c"].reason, "dependency_failed")

    def test_policy_violation_not_retried(self) -> None:
        rt, store, spec = self.build(
            [{"id": "a", "executor": "shell", "command": "rm -rf .", "max_attempts": 5}]
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.FAILED)
        self.assertEqual(state.steps["a"].attempts, 1)
        self.assertEqual(state.steps["a"].error["code"], "policy_violation")
        self.assertFalse(state.steps["a"].error["retryable"])

    def test_exception_mapped_not_crash(self) -> None:
        def boom(step, ctx):
            raise RuntimeError("内部炸了")

        rt, store, spec = self.build([P("a", "boom")], {"boom": boom})
        state = rt.run()
        self.assertIs(state.status, RunStatus.FAILED)
        self.assertEqual(state.steps["a"].error["code"], "exception")


class TestVerify(RuntimeTestBase):
    def test_verify_success(self) -> None:
        rt, store, spec = self.build(
            [P("a", "hit", verify=py_cmd("print('ok')"))], {"hit": counter_target([])}
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.steps["a"].evidence["verification"], "command")

    def test_verify_failure(self) -> None:
        rt, store, spec = self.build(
            [P("a", "hit", verify=py_cmd("import sys; sys.exit(1)"), max_attempts=1)],
            {"hit": counter_target([])},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.FAILED)
        self.assertEqual(state.steps["a"].error["code"], "verify_failed")


class TestBudget(RuntimeTestBase):
    def test_max_steps_aborts(self) -> None:
        rt, store, spec = self.build(
            [P("a", "hit"), P("b", "hit"), P("c", "hit")],
            {"hit": counter_target([])},
            boundaries={"max_steps": 1},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.ABORTED)
        self.assertEqual(state.counters["steps_executed"], 1)
        self.assertIn("max_steps", state.boundaries_hit)
        self.assertIs(state.steps["b"].status, StepStatus.CANCELLED)

    def test_max_wall_aborts(self) -> None:
        rt, store, spec = self.build(
            [P("a", "hit")],
            {"hit": counter_target([])},
            boundaries={"max_wall_seconds": 0.0001},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.ABORTED)
        self.assertIn("max_wall_seconds", state.boundaries_hit)


class TestDryRun(RuntimeTestBase):
    def test_dry_run_does_not_execute(self) -> None:
        calls: list[str] = []
        rt, store, spec = self.build(
            [P("a", "hit")], {"hit": counter_target(calls)}, dry_run=True
        )
        state = rt.run()
        self.assertEqual(calls, [])
        self.assertIs(state.status, RunStatus.SUCCEEDED)
        self.assertTrue(state.steps["a"].evidence["simulated"])
        self.assertEqual(state.steps["a"].evidence["verification"], "dry_run")


class TestInterruptRecovery(RuntimeTestBase):
    def test_resume_does_not_rerun_finished_step(self) -> None:
        """构造真实崩溃现场：a 跑完（journal 已记）但 state 落后；b 崩在半路；c 未开始。"""
        spec = make_spec(
            [
                P("a", "hit"),
                P("b", "hit", deps=["a"]),
                P("c", "hit", deps=["b"]),
            ]
        )
        store = RunStore(self.root / "run")
        store.initialize(spec)
        state = RunState.create(spec)
        state.steps["a"].status = StepStatus.RUNNING  # state 落后于 journal
        state.steps["b"].status = StepStatus.RUNNING  # 崩在这里
        store.save_state(state)
        store.append_event({"event": "step_started", "step_id": "a", "key": "step:a", "attempt": 1})
        store.append_event(
            {"event": "step_finished", "step_id": "a", "key": "step:a", "ok": True, "duration_s": 0.1}
        )
        store.append_event({"event": "step_started", "step_id": "b", "key": "step:b", "attempt": 1})

        calls: list[str] = []
        rt = self.reopen(store, {"hit": counter_target(calls)})
        state2 = rt.resume()

        self.assertIs(state2.status, RunStatus.SUCCEEDED)
        # a 由 journal 判定已完成 —— 绝不重跑；b、c 依次重跑
        self.assertEqual(calls, ["hit:b", "hit:c"])
        self.assertEqual(state2.counters["resumes"], 1)

    def test_resume_refuses_terminal_run(self) -> None:
        rt, store, spec = self.build([P("a", "hit")], {"hit": counter_target([])})
        rt.run()
        with self.assertRaises(StateError):
            self.reopen(store, {"hit": counter_target([])}).resume()


if __name__ == "__main__":
    unittest.main()
