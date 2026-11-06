"""人工确认与暂停/恢复测试。重点：暂停不是失败，驳回要能带走下游。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from total_agent.human import ApprovalQueue
from total_agent.model import RunStatus, StepStatus
from total_agent.policy import Boundaries, Policy
from total_agent.runtime import Runtime
from total_agent.store import RunStore

from _helpers import counter_target, make_spec


def P(sid, target, deps=(), **kw):
    raw = {"id": sid, "executor": "python", "target": target, "depends_on": list(deps)}
    raw.update(kw)
    return raw


class QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.q = ApprovalQueue(Path(self._tmp.name) / "approvals")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_request_and_get(self) -> None:
        ap = self.q.request("a", "要确认吗")
        self.assertEqual(ap.status, "pending")
        self.assertEqual(self.q.get("a").question, "要确认吗")

    def test_duplicate_request_does_not_duplicate(self) -> None:
        self.q.request("a", "第一次")
        self.q.request("a", "第二次")
        self.assertEqual(self.q.get("a").question, "第一次")
        self.assertEqual(len(self.q.pending()), 1)

    def test_approve(self) -> None:
        self.q.request("a", "q")
        ap = self.q.decide("a", approved=True, note="同意")
        self.assertEqual(ap.status, "approved")
        self.assertEqual(self.q.pending(), [])

    def test_reject(self) -> None:
        self.q.request("a", "q")
        ap = self.q.decide("a", approved=False, note="不行")
        self.assertEqual(ap.status, "rejected")

    def test_decide_twice_rejected(self) -> None:
        self.q.request("a", "q")
        self.q.decide("a", approved=True)
        with self.assertRaises(ValueError):
            self.q.decide("a", approved=False)

    def test_decide_unknown(self) -> None:
        with self.assertRaises(KeyError):
            self.q.decide("ghost", approved=True)

    def test_unsafe_step_id_sanitized(self) -> None:
        self.q.request("../../etc/passwd", "q")
        names = [p.name for p in self.q.dir.glob("*.json")]
        self.assertEqual(len(names), 1)
        self.assertNotIn("..", names[0])


class ApprovalFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runtime(self, spec, calls):
        store = RunStore(self.root / "run")
        return (
            Runtime(
                spec,
                store,
                Policy(Boundaries(workspace_root=self.root)),
                python_registry={"hit": counter_target(calls)},
            ),
            store,
        )

    def test_block_then_approve_then_resume(self) -> None:
        calls: list[str] = []
        spec = make_spec([P("a", "hit", requires_approval=True), P("b", "hit", deps=["a"])])
        rt, store = self._runtime(spec, calls)

        state = rt.run()
        self.assertIs(state.status, RunStatus.PAUSED)
        self.assertIs(state.steps["a"].status, StepStatus.BLOCKED)
        self.assertEqual(calls, [], "挂起时不应执行任何步骤")
        self.assertEqual(len(list(store.approvals_dir.glob("*.json"))), 1)

        ApprovalQueue(store.approvals_dir).decide("a", approved=True, note="ok")
        state2 = self._runtime(store.load_spec(), calls)[0].resume()
        self.assertIs(state2.status, RunStatus.SUCCEEDED)
        self.assertEqual(calls, ["hit:a", "hit:b"])

    def test_reject_cancels_and_skips_downstream(self) -> None:
        calls: list[str] = []
        spec = make_spec([P("a", "hit", requires_approval=True), P("b", "hit", deps=["a"])])
        rt, store = self._runtime(spec, calls)
        rt.run()

        ApprovalQueue(store.approvals_dir).decide("a", approved=False, note="不要")
        state2 = self._runtime(store.load_spec(), calls)[0].resume()

        self.assertIs(state2.steps["a"].status, StepStatus.CANCELLED)
        self.assertIs(state2.steps["b"].status, StepStatus.SKIPPED)
        self.assertIs(state2.status, RunStatus.FAILED, "没有任何步骤成功就不能报成功")
        self.assertEqual(calls, [])

    def test_global_require_approval(self) -> None:
        calls: list[str] = []
        spec = make_spec(
            [P("a", "hit")], boundaries={"require_approval": True}
        )
        store = RunStore(self.root / "run")
        rt = Runtime(
            spec,
            store,
            Policy(Boundaries.from_dict({"require_approval": True}, self.root)),
            python_registry={"hit": counter_target(calls)},
        )
        state = rt.run()
        self.assertIs(state.status, RunStatus.PAUSED)
        self.assertIs(state.steps["a"].status, StepStatus.BLOCKED)

    def test_resume_without_decision_stays_paused(self) -> None:
        calls: list[str] = []
        spec = make_spec([P("a", "hit", requires_approval=True)])
        rt, store = self._runtime(spec, calls)
        rt.run()
        state2 = self._runtime(store.load_spec(), calls)[0].resume()
        self.assertIs(state2.status, RunStatus.PAUSED)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
