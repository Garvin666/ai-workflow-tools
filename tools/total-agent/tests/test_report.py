"""结构化结果测试。重点：需求五项必须都在，且 next_actions 要指向具体动作。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from total_agent.model import RunState, RunStatus, StepStatus
from total_agent.report import build_report, derive_next_actions, render_markdown

from _helpers import make_spec

REQUIRED_KEYS = (
    "run_id",
    "goal",
    "status",
    "final_outputs",
    "completed_steps",
    "pending_steps",
    "anomalies",
    "next_actions",
)


def _spec():
    return make_spec(
        [
            {"id": "a", "executor": "shell", "command": "echo a", "artifacts": ["out/a.txt"]},
            {"id": "b", "executor": "shell", "command": "echo b", "depends_on": ["a"]},
            {"id": "c", "executor": "shell", "command": "echo c", "depends_on": ["b"]},
        ]
    )


class ReportShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = _spec()
        self.state = RunState.create(self.spec)

    def test_required_keys(self) -> None:
        for key in REQUIRED_KEYS:
            self.assertIn(key, build_report(self.state, self.spec))

    def test_json_serializable(self) -> None:
        text = json.dumps(build_report(self.state, self.spec), ensure_ascii=False)
        self.assertIn("next_actions", text)

    def test_completed_vs_pending(self) -> None:
        self.state.steps["a"].status = StepStatus.SUCCEEDED
        self.state.steps["a"].evidence = {
            "exit_code": 0,
            "stdout_tail": "done",
            "verification": "none",
        }
        self.state.steps["b"].status = StepStatus.FAILED
        self.state.steps["b"].error = {
            "code": "nonzero_exit",
            "message": "boom",
            "retryable": True,
        }
        self.state.steps["c"].status = StepStatus.SKIPPED
        self.state.steps["c"].reason = "dependency_failed"

        report = build_report(self.state, self.spec)
        self.assertEqual([x["id"] for x in report["completed_steps"]], ["a"])
        self.assertEqual([x["id"] for x in report["pending_steps"]], ["b", "c"])
        self.assertEqual([x["step"] for x in report["anomalies"]], ["b"])

    def test_final_outputs_carries_artifacts(self) -> None:
        self.state.steps["a"].status = StepStatus.SUCCEEDED
        self.state.steps["a"].evidence = {"exit_code": 0, "stdout_tail": "ok", "verification": "none"}
        report = build_report(self.state, self.spec)
        self.assertEqual(report["final_outputs"][0]["artifacts"], ["out/a.txt"])


class NextActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = _spec()

    def test_success(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.SUCCEEDED
        for st in state.steps.values():
            st.status = StepStatus.SUCCEEDED
        actions = derive_next_actions(state, self.spec)
        self.assertEqual(len(actions), 1)
        self.assertIn("已完成", actions[0])

    def test_failed_points_to_specific_advice(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.FAILED
        state.steps["a"].status = StepStatus.FAILED
        state.steps["a"].error = {"code": "timeout", "message": "t", "retryable": True}
        actions = derive_next_actions(state, self.spec)
        joined = " ".join(actions)
        self.assertIn("timeout_s", joined)

    def test_paused_mentions_approve_command(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.PAUSED
        state.steps["a"].status = StepStatus.BLOCKED
        actions = derive_next_actions(state, self.spec)
        self.assertTrue(any("approve" in a for a in actions))

    def test_aborted_mentions_boundary(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.ABORTED
        state.boundaries_hit = ["max_steps"]
        actions = derive_next_actions(state, self.spec)
        self.assertTrue(any("max_steps" in a for a in actions))

    def test_skipped_mentioned(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.FAILED
        state.steps["b"].status = StepStatus.SKIPPED
        actions = derive_next_actions(state, self.spec)
        self.assertTrue(any("被跳过" in a for a in actions))

    def test_unknown_error_code_has_fallback(self) -> None:
        state = RunState.create(self.spec)
        state.status = RunStatus.FAILED
        state.steps["a"].status = StepStatus.FAILED
        state.steps["a"].error = {"code": "weird", "message": "x", "retryable": False}
        actions = derive_next_actions(state, self.spec)
        self.assertTrue(any("weird" in a for a in actions))


class MarkdownTest(unittest.TestCase):
    def test_all_five_sections_present(self) -> None:
        spec = _spec()
        state = RunState.create(spec)
        state.steps["a"].status = StepStatus.SUCCEEDED
        state.steps["a"].evidence = {"exit_code": 0, "stdout_tail": "ok", "verification": "none"}
        state.steps["b"].status = StepStatus.FAILED
        state.steps["b"].error = {"code": "nonzero_exit", "message": "boom", "retryable": True}
        md = render_markdown(build_report(state, spec))
        for heading in ("最终产出", "已完成步骤", "未完成项", "异常原因", "后续处理建议"):
            self.assertIn(heading, md)

    def test_pipe_escaped_in_table(self) -> None:
        spec = _spec()
        state = RunState.create(spec)
        state.steps["a"].status = StepStatus.FAILED
        state.steps["a"].error = {"code": "x", "message": "a|b", "retryable": False}
        md = render_markdown(build_report(state, spec))
        self.assertIn("a\\|b", md)


class StoreIntegrationTest(unittest.TestCase):
    def test_pending_approvals_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from total_agent.human import ApprovalQueue
            from total_agent.store import RunStore

            spec = _spec()
            store = RunStore(Path(tmp) / "run")
            store.initialize(spec)
            state = RunState.create(spec)
            store.save_state(state)
            ApprovalQueue(store.approvals_dir).request("a", "要不要执行")

            report = build_report(state, spec, store)
            self.assertEqual(report["pending_approvals"][0]["step_id"], "a")


if __name__ == "__main__":
    unittest.main()
