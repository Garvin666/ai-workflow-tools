"""测试公共工具。不以 test_ 开头，不会被 discover 当作用例。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from total_agent.executors import ExecResult, PythonExecutor
from total_agent.model import Step, TaskSpec


def py_cmd(code: str) -> str:
    """构造一条跨平台的 python -c 命令（路径带引号，防空格）。"""
    return f'"{sys.executable}" -c "{code}"'


def make_spec(
    steps: list[dict[str, Any]],
    *,
    goal: str = "测试目标",
    boundaries: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "goal": goal,
            "steps": steps,
            "boundaries": boundaries or {},
            "inputs": inputs or {},
            "constraints": ["仅测试"],
            "acceptance": ["用例通过"],
        }
    )


def counter_target(store: list[str], name: str = "hit", *, ok: bool = True) -> Callable:
    """生成一个可注册的 python 步骤目标，记录调用次数。"""

    def _fn(step: Step, ctx: dict[str, Any]) -> ExecResult:
        store.append(f"{name}:{step.id}")
        return ExecResult(ok=ok, exit_code=0 if ok else 1, stdout=f"{name}:{step.id}")

    return _fn


def failing_target(store: list[str], times: int) -> Callable:
    """前 times 次失败，之后成功 —— 用于验证重试语义。"""

    def _fn(step: Step, ctx: dict[str, Any]) -> ExecResult:
        store.append(step.id)
        if len(store) <= times:
            return ExecResult(ok=False, exit_code=1, stderr="boom", error_kind="nonzero_exit")
        return ExecResult(ok=True, exit_code=0, stdout="ok")

    return _fn


def registry(**targets: Callable) -> PythonExecutor:
    return PythonExecutor(dict(targets))


def write_file(root: Path, rel: str, text: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
