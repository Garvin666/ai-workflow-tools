"""行为边界测试。重点：逃逸路径与串联命令必须被拦住。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from total_agent.errors import BudgetExceeded, PolicyViolation
from total_agent.model import RunState, StepState
from total_agent.policy import Boundaries, Policy, command_segments


class PolicyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def policy(self, **kw) -> Policy:
        return Policy(Boundaries(workspace_root=self.root, **kw))


class TestPathBoundary(PolicyTestBase):
    def test_inside_ok(self) -> None:
        p = self.policy()
        self.assertTrue(str(p.check_path("artifacts/a.txt")).startswith(str(self.root)))

    def test_relative_escape_rejected(self) -> None:
        p = self.policy()
        with self.assertRaises(PolicyViolation):
            p.check_path("../outside.txt")

    def test_absolute_outside_rejected(self) -> None:
        p = self.policy()
        outside = Path(tempfile.gettempdir()).resolve() / "definitely-outside.txt"
        with self.assertRaises(PolicyViolation):
            p.check_path(outside)

    def test_extra_allowed_path(self) -> None:
        extra = Path(tempfile.mkdtemp()).resolve()
        try:
            p = self.policy(extra_allowed_paths=(str(extra),))
            p.check_path(extra / "ok.txt")
        finally:
            extra.rmdir()

    def test_sibling_prefix_not_confused(self) -> None:
        """root=/tmp/abc 时 /tmp/abcd 不算在内 —— 前缀比较必须带分隔符。"""
        sibling = Path(str(self.root) + "-sibling")
        p = self.policy()
        with self.assertRaises(PolicyViolation):
            p.check_path(sibling / "x.txt")


class TestCommandBoundary(PolicyTestBase):
    def test_normal_command_ok(self) -> None:
        self.policy().check_command("echo hello")

    def test_denied_builtin(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("rm -rf /tmp/x")

    def test_denied_after_and(self) -> None:
        """串联命令里的危险段必须也被拦。"""
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("echo ok && rm -rf .")

    def test_denied_after_pipe(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("echo ok | reg query HKLM")

    def test_whitelist_blocks_unknown(self) -> None:
        p = self.policy(allowed_commands=("echo", "python*"))
        p.check_command("echo hi")
        with self.assertRaises(PolicyViolation):
            p.check_command("curl http://x")

    def test_whitelist_glob(self) -> None:
        p = self.policy(allowed_commands=("py*",))
        p.check_command("python -c pass")

    def test_denied_beats_whitelist(self) -> None:
        p = self.policy(allowed_commands=("*",))
        with self.assertRaises(PolicyViolation):
            p.check_command("shutdown /s")

    def test_empty_command(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("   ")

    def test_exe_suffix_stripped(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("TASKKILL.EXE /im x")

    def test_env_assignment_prefix(self) -> None:
        """FOO=1 rm -rf 这类前缀不能绕过检测。"""
        with self.assertRaises(PolicyViolation):
            self.policy().check_command("FOO=1 rm -rf .")


class TestCommandSegments(unittest.TestCase):
    def test_split(self) -> None:
        segs = command_segments("a && b || c | d ; e")
        self.assertEqual([s.strip() for s in segs if s.strip()], ["a", "b", "c", "d", "e"])


class TestBudget(PolicyTestBase):
    def _state(self, **counters) -> RunState:
        state = RunState(run_id="r", goal="g", steps={"a": StepState(id="a")})
        state.counters.update(counters)
        return state

    def test_steps_limit(self) -> None:
        p = self.policy(max_steps=3)
        state = self._state(steps_executed=4)
        with self.assertRaises(BudgetExceeded):
            p.check_budget(state, elapsed_s=1.0)
        self.assertIn("max_steps", state.boundaries_hit)

    def test_wall_limit(self) -> None:
        p = self.policy(max_wall_seconds=10)
        state = self._state()
        with self.assertRaises(BudgetExceeded):
            p.check_budget(state, elapsed_s=11)
        self.assertIn("max_wall_seconds", state.boundaries_hit)

    def test_attempts_limit(self) -> None:
        p = self.policy(max_total_attempts=5)
        state = self._state(total_attempts=6)
        with self.assertRaises(BudgetExceeded):
            p.check_budget(state, elapsed_s=1.0)
        self.assertIn("max_total_attempts", state.boundaries_hit)

    def test_within_budget(self) -> None:
        p = self.policy(max_steps=10, max_wall_seconds=60, max_total_attempts=10)
        p.check_budget(self._state(steps_executed=1, total_attempts=1), elapsed_s=1.0)


class TestBoundariesConfig(PolicyTestBase):
    def test_defaults(self) -> None:
        b = Boundaries(workspace_root=self.root)
        self.assertEqual(b.max_steps, 1000)
        self.assertIn("rm", b.denied_commands)
        self.assertFalse(b.dry_run)

    def test_override(self) -> None:
        b = Boundaries.from_dict({"max_steps": 7, "require_approval": True}, self.root)
        self.assertEqual(b.max_steps, 7)
        self.assertTrue(b.require_approval)

    def test_explicit_empty_denied(self) -> None:
        b = Boundaries.from_dict({"denied_commands": []}, self.root)
        self.assertEqual(b.denied_commands, ())

    def test_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            Boundaries(workspace_root=self.root, max_steps=0)
        with self.assertRaises(ValueError):
            Boundaries(workspace_root=self.root, max_wall_seconds=0)


if __name__ == "__main__":
    unittest.main()
