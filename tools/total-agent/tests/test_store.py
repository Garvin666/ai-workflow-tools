"""持久化与恢复测试。重点：journal 是真相源，状态文件落后于事实时要能纠正回来。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from total_agent.errors import StateError
from total_agent.model import RunState, StepState, StepStatus, TaskSpec
from total_agent.store import RunStore

from _helpers import make_spec


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "run-001"
        self.store = RunStore(self.dir)
        self.spec = make_spec(
            [
                {"id": "a", "executor": "shell", "command": "echo a"},
                {"id": "b", "executor": "shell", "command": "echo b", "depends_on": ["a"]},
            ]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def init(self) -> RunState:
        self.store.initialize(self.spec)
        state = RunState.create(self.spec)
        self.store.save_state(state)
        return state


class TestLifecycle(StoreTestBase):
    def test_initialize_creates_files(self) -> None:
        self.store.initialize(self.spec)
        self.assertTrue(self.store.spec_path.exists())
        self.assertTrue(self.store.journal_path.exists())
        self.assertTrue(self.store.approvals_dir.is_dir())
        self.assertTrue(self.store.artifacts_dir.is_dir())

    def test_double_initialize_rejected(self) -> None:
        self.store.initialize(self.spec)
        with self.assertRaises(StateError):
            self.store.initialize(self.spec)

    def test_load_spec_roundtrip(self) -> None:
        self.store.initialize(self.spec)
        loaded = self.store.load_spec()
        self.assertEqual(loaded.goal, self.spec.goal)
        self.assertEqual([s.id for s in loaded.steps], ["a", "b"])

    def test_exists(self) -> None:
        self.assertFalse(self.store.exists())
        self.init()
        self.assertTrue(self.store.exists())

    def test_no_tmp_left_after_state_write(self) -> None:
        state = self.init()
        self.store.save_state(state)
        leftovers = list(self.dir.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"原子写不应留下临时文件：{leftovers}")

    def test_load_state_corrupt(self) -> None:
        state = self.init()
        self.store.save_state(state)
        self.store.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateError):
            self.store.load_state()


class TestJournal(StoreTestBase):
    def test_append_and_read(self) -> None:
        self.init()
        self.store.append_event({"event": "step_started", "step_id": "a", "key": "step:a"})
        self.store.append_event(
            {"event": "step_finished", "step_id": "a", "key": "step:a", "ok": True}
        )
        events = self.store.read_events()
        self.assertEqual(len(events), 3)  # 含 run_created
        self.assertEqual(events[-1]["event"], "step_finished")
        self.assertTrue(events[-1]["ts"])

    def test_finished_ok_keys(self) -> None:
        self.init()
        self.store.append_event(
            {"event": "step_finished", "step_id": "a", "key": "k-a", "ok": True}
        )
        self.store.append_event(
            {"event": "step_finished", "step_id": "b", "key": "k-b", "ok": False}
        )
        self.assertEqual(self.store.finished_ok_keys(), {"k-a"})

    def test_truncated_tail_is_reported_not_swallowed(self) -> None:
        self.init()
        with open(self.store.journal_path, "a", encoding="utf-8") as f:
            f.write('{"event": "step_finished", "step_id"')  # 崩在写一半
        events = self.store.read_events()
        self.assertTrue(any(e["event"] == "journal_corrupt_line" for e in events))


class TestRepair(StoreTestBase):
    def test_running_with_finished_in_journal_becomes_succeeded(self) -> None:
        state = self.init()
        state.steps["a"].status = StepStatus.RUNNING
        self.store.save_state(state)
        self.store.append_event(
            {"event": "step_started", "step_id": "a", "key": "step:a"}
        )
        self.store.append_event(
            {"event": "step_finished", "step_id": "a", "key": "step:a", "ok": True}
        )

        notes = self.store.repair(state)
        self.assertIs(state.steps["a"].status, StepStatus.SUCCEEDED)
        self.assertEqual(state.steps["a"].reason, "recovered_from_journal")
        self.assertTrue(any("succeeded" in n for n in notes))

    def test_running_without_finish_becomes_interrupted(self) -> None:
        state = self.init()
        state.steps["a"].status = StepStatus.RUNNING
        self.store.save_state(state)
        self.store.append_event(
            {"event": "step_started", "step_id": "a", "key": "step:a"}
        )

        notes = self.store.repair(state)
        self.assertIs(state.steps["a"].status, StepStatus.INTERRUPTED)
        self.assertTrue(any("interrupted" in n for n in notes))

    def test_completed_steps_untouched(self) -> None:
        state = self.init()
        state.steps["a"].status = StepStatus.SUCCEEDED
        state.steps["b"].status = StepStatus.SKIPPED
        self.store.repair(state)
        self.assertIs(state.steps["a"].status, StepStatus.SUCCEEDED)
        self.assertIs(state.steps["b"].status, StepStatus.SKIPPED)

    def test_no_repair_needed(self) -> None:
        state = self.init()
        self.assertEqual(self.store.repair(state), [])

    def test_repair_uses_idempotency_key_form(self) -> None:
        """state 只记得 key（evidence 里），journal 也按 key 记 —— 两者要能对上。"""
        state = self.init()
        st = state.steps["a"]
        st.status = StepStatus.RUNNING
        st.evidence = {"idempotency_key": "custom-key"}
        self.store.save_state(state)
        self.store.append_event(
            {"event": "step_finished", "step_id": "a", "key": "custom-key", "ok": True}
        )
        self.store.repair(state)
        self.assertIs(state.steps["a"].status, StepStatus.SUCCEEDED)


class TestArtifacts(StoreTestBase):
    def test_write_artifact(self) -> None:
        self.store.initialize(self.spec)
        p = self.store.write_artifact_text("sub/out.txt", "hello")
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(encoding="utf-8"), "hello")

    def test_escape_rejected(self) -> None:
        self.store.initialize(self.spec)
        with self.assertRaises(StateError):
            self.store.artifact_path("../escape.txt")

    def test_report_write(self) -> None:
        self.init()
        path = self.store.write_report({"status": "succeeded"})
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
