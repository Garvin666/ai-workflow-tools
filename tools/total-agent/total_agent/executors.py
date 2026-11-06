"""[自研工具] total-agent · 执行器层

自研标识：Garvin666 自研工具 total-agent / 用途：把「一步做什么」落到真实动作 / 适用场景：shell 脚本与代码流程。
仓库链接：待推送。

四类执行器共用同一个返回结构 ExecResult，失败原因用 error_kind 归一化 ——
上层只按 error_kind 决定重试/暂停/终止，不需要认识每种异常。
"""

from __future__ import annotations

import abc
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .model import Step


@dataclass
class ExecResult:
    ok: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    error_kind: str = ""
    simulated: bool = False
    needs_info: str = ""

    def to_evidence(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "stdout_tail": self.stdout[-800:],
            "stderr_tail": self.stderr[-800:],
            "simulated": self.simulated,
            "error_kind": self.error_kind,
        }


class Executor(Protocol):
    name: str

    def run(self, step: Step, cwd: Path, timeout_s: float) -> ExecResult: ...


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #


class ShellExecutor:
    """把命令交给系统 shell。

    DESIGN §11 已登记：命令字符串具备 shell 权限（支持管道与重定向），
    边界靠 policy 的逐段首 token 校验兜底 —— 那是防误操作，不是安全沙箱。
    """

    name = "shell"

    def run(self, step: Step, cwd: Path, timeout_s: float) -> ExecResult:
        return run_shell(step.command, cwd=cwd, timeout_s=timeout_s)


def run_shell(command: str, *, cwd: Path, timeout_s: float) -> ExecResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecResult(
            ok=False,
            exit_code=-1,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_s=time.monotonic() - start,
            error_kind="timeout",
        )
    except FileNotFoundError as exc:
        return ExecResult(
            ok=False,
            exit_code=127,
            stderr=str(exc),
            duration_s=time.monotonic() - start,
            error_kind="not_found",
        )
    except OSError as exc:
        return ExecResult(
            ok=False,
            exit_code=126,
            stderr=str(exc),
            duration_s=time.monotonic() - start,
            error_kind="os_error",
        )

    duration = time.monotonic() - start
    ok = proc.returncode == 0
    return ExecResult(
        ok=ok,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=duration,
        error_kind="" if ok else "nonzero_exit",
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --------------------------------------------------------------------------- #
# python 可调用
# --------------------------------------------------------------------------- #

CallableStep = Callable[[Step, dict[str, Any]], Any]


class PythonExecutor:
    """调用注册表里的可调用对象。测试与嵌入式使用。"""

    name = "python"

    def __init__(self, registry: dict[str, CallableStep] | None = None) -> None:
        self.registry: dict[str, CallableStep] = dict(registry or {})

    def register(self, name: str, fn: CallableStep) -> None:
        self.registry[name] = fn

    def run(self, step: Step, cwd: Path, timeout_s: float) -> ExecResult:
        fn = self.registry.get(step.target)
        if fn is None:
            return ExecResult(
                ok=False,
                exit_code=127,
                stderr=f"未注册的 python 目标：{step.target}",
                error_kind="not_found",
            )
        start = time.monotonic()
        try:
            raw = fn(step, {"cwd": str(cwd), "args": step.args})
        except Exception as exc:  # noqa: BLE001 - 用户代码，什么都能抛
            return ExecResult(
                ok=False,
                exit_code=1,
                stderr=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - start,
                error_kind="exception",
            )
        duration = time.monotonic() - start
        return coalesce_result(raw, duration)


def coalesce_result(raw: Any, duration: float = 0.0) -> ExecResult:
    """把用户返回的任意形态归一为 ExecResult。"""
    if isinstance(raw, ExecResult):
        if raw.duration_s == 0.0:
            raw.duration_s = duration
        return raw
    if raw is None:
        return ExecResult(ok=True, duration_s=duration)
    if isinstance(raw, bool):
        return ExecResult(ok=raw, exit_code=0 if raw else 1, duration_s=duration,
                          error_kind="" if raw else "nonzero_exit")
    if isinstance(raw, str):
        return ExecResult(ok=True, stdout=raw, duration_s=duration)
    if isinstance(raw, dict):
        ok = bool(raw.get("ok", True))
        return ExecResult(
            ok=ok,
            exit_code=int(raw.get("exit_code", 0 if ok else 1)),
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            duration_s=duration,
            error_kind=str(raw.get("error_kind", "" if ok else "nonzero_exit")),
            needs_info=str(raw.get("needs_info", "")),
        )
    return ExecResult(ok=True, stdout=str(raw), duration_s=duration)


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #


class DryRunExecutor:
    """只走流程，不产生副作用。报告里 simulated=True，绝不冒充真实执行。"""

    name = "dry-run"

    def __init__(self, inner: Executor | None = None) -> None:
        self.inner = inner

    def run(self, step: Step, cwd: Path, timeout_s: float) -> ExecResult:
        return ExecResult(
            ok=True,
            exit_code=0,
            stdout=f"[dry-run] 跳过执行：{step.command or step.target}",
            duration_s=0.0,
            simulated=True,
        )


# --------------------------------------------------------------------------- #
# LLM 适配器（接口 + mock）
# --------------------------------------------------------------------------- #


class LLMExecutor(abc.ABC):
    """真实模型接入点。子类实现 complete()，本类负责把它包成 ExecResult。

    刻意不内置任何厂商 SDK：凭据与网络策略应留在使用方，不进框架。
    """

    name = "llm"

    @abc.abstractmethod
    def complete(self, prompt: str, step: Step) -> str:
        ...

    def run(self, step: Step, cwd: Path, timeout_s: float) -> ExecResult:
        start = time.monotonic()
        prompt = step.command or step.action
        try:
            text = self.complete(prompt, step)
        except Exception as exc:  # noqa: BLE001
            return ExecResult(
                ok=False,
                exit_code=1,
                stderr=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - start,
                error_kind="llm_error",
            )
        return ExecResult(
            ok=True, stdout=text, duration_s=time.monotonic() - start
        )


class MockLLMExecutor(LLMExecutor):
    """确定性 mock：同样的输入给同样的输出。用于测试与演示。"""

    def __init__(self, response: str = "[mock-llm] ok") -> None:
        self.response = response
        self.calls: list[str] = []

    def complete(self, prompt: str, step: Step) -> str:
        self.calls.append(prompt)
        return f"{self.response}｜step={step.id}"


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def build_executor(
    kind: str,
    *,
    python_registry: dict[str, CallableStep] | None = None,
    llm: LLMExecutor | None = None,
) -> Executor:
    if kind == "shell":
        return ShellExecutor()
    if kind == "python":
        return PythonExecutor(python_registry)
    if kind == "llm":
        return llm or MockLLMExecutor()
    if kind == "none":
        return DryRunExecutor()
    raise ValueError(f"未知执行器类型：{kind}")
