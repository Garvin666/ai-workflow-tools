"""执行器测试。重点：所有失败都要归一成 error_kind，上层不认异常类型。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from total_agent.executors import (
    DryRunExecutor,
    ExecResult,
    MockLLMExecutor,
    PythonExecutor,
    ShellExecutor,
    build_executor,
    coalesce_result,
    run_shell,
)
from total_agent.model import Step

from _helpers import counter_target, py_cmd


def shell_step(cmd: str, **kw) -> Step:
    raw = {"id": "s", "executor": "shell", "command": cmd}
    raw.update(kw)
    return Step.from_dict(raw)


class ShellTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success(self) -> None:
        res = run_shell(py_cmd("print(42)"), cwd=self.cwd, timeout_s=30)
        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("42", res.stdout)

    def test_nonzero_exit(self) -> None:
        res = run_shell(py_cmd("import sys; sys.exit(3)"), cwd=self.cwd, timeout_s=30)
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 3)
        self.assertEqual(res.error_kind, "nonzero_exit")

    def test_timeout(self) -> None:
        res = run_shell(py_cmd("import time; time.sleep(5)"), cwd=self.cwd, timeout_s=0.4)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "timeout")

    def test_cwd_is_respected(self) -> None:
        res = run_shell(py_cmd("import os; print(os.getcwd())"), cwd=self.cwd, timeout_s=30)
        self.assertIn(self.cwd.name, res.stdout)

    def test_shell_executor_delegates(self) -> None:
        ex = ShellExecutor()
        res = ex.run(shell_step(py_cmd("print('x')")), self.cwd, 30)
        self.assertTrue(res.ok)


class PythonExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = Path(tempfile.gettempdir())
        self.calls: list[str] = []

    def test_registered_target(self) -> None:
        ex = PythonExecutor({"hit": counter_target(self.calls)})
        res = ex.run(Step.from_dict({"id": "p", "executor": "python", "target": "hit"}), self.cwd, 5)
        self.assertTrue(res.ok)
        self.assertEqual(self.calls, ["hit:p"])

    def test_missing_target(self) -> None:
        ex = PythonExecutor({})
        res = ex.run(Step.from_dict({"id": "p", "executor": "python", "target": "nope"}), self.cwd, 5)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "not_found")

    def test_exception_mapped(self) -> None:
        def boom(step, ctx):
            raise ValueError("炸了")

        ex = PythonExecutor({"boom": boom})
        res = ex.run(Step.from_dict({"id": "p", "executor": "python", "target": "boom"}), self.cwd, 5)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "exception")
        self.assertIn("炸了", res.stderr)

    def test_needs_info_passthrough(self) -> None:
        ex = PythonExecutor({"ask": lambda s, c: {"ok": False, "needs_info": "要确认口径"}})
        res = ex.run(Step.from_dict({"id": "p", "executor": "python", "target": "ask"}), self.cwd, 5)
        self.assertEqual(res.needs_info, "要确认口径")


class CoalesceTest(unittest.TestCase):
    def test_none(self) -> None:
        self.assertTrue(coalesce_result(None).ok)

    def test_bool(self) -> None:
        self.assertTrue(coalesce_result(True).ok)
        self.assertFalse(coalesce_result(False).ok)

    def test_str(self) -> None:
        r = coalesce_result("hello")
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, "hello")

    def test_dict(self) -> None:
        r = coalesce_result({"ok": False, "exit_code": 9, "stderr": "bad"})
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 9)
        self.assertEqual(r.error_kind, "nonzero_exit")

    def test_exec_result_kept(self) -> None:
        given = ExecResult(ok=True, stdout="x", duration_s=0.0)
        r = coalesce_result(given, duration=1.5)
        self.assertEqual(r.duration_s, 1.5)


class DryRunTest(unittest.TestCase):
    def test_simulated(self) -> None:
        res = DryRunExecutor().run(shell_step("echo hi"), Path(tempfile.gettempdir()), 5)
        self.assertTrue(res.ok)
        self.assertTrue(res.simulated)
        self.assertEqual(res.exit_code, 0)


class MockLLMTest(unittest.TestCase):
    def test_deterministic(self) -> None:
        llm = MockLLMExecutor("答案")
        step = Step.from_dict({"id": "l", "executor": "llm", "command": "prompt"})
        r1 = llm.run(step, Path(tempfile.gettempdir()), 5)
        r2 = llm.run(step, Path(tempfile.gettempdir()), 5)
        self.assertEqual(r1.stdout, r2.stdout)
        self.assertIn("答案", r1.stdout)
        self.assertEqual(len(llm.calls), 2)


class FactoryTest(unittest.TestCase):
    def test_build(self) -> None:
        self.assertIsInstance(build_executor("shell"), ShellExecutor)
        self.assertIsInstance(build_executor("python"), PythonExecutor)
        self.assertIsInstance(build_executor("llm"), MockLLMExecutor)
        self.assertIsInstance(build_executor("none"), DryRunExecutor)

    def test_unknown(self) -> None:
        with self.assertRaises(ValueError):
            build_executor("quantum")


if __name__ == "__main__":
    unittest.main()
