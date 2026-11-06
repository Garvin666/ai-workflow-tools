"""[自研工具] total-agent · 行为边界

自研标识：Garvin666 自研工具 total-agent / 用途：可配置的执行边界 / 适用场景：自主代理的失控防护。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent。

⚠️ 如实标注：这不是安全沙箱。它挡的是「误操作」与「跑飞」，挡不住蓄意绕过
（命令最终仍交给系统 shell）。真正的隔离要靠容器/账户权限，不在本模块射程内。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import BudgetExceeded, PolicyViolation
from .model import RunState

# 拒绝对话式确认的命令一律不进白名单：这些动作在自动化里没有「撤销」。
DEFAULT_DENIED: tuple[str, ...] = (
    "rm", "rmdir", "del", "erase", "format", "mkfs", "diskpart", "fdisk",
    "shutdown", "reboot", "halt", "poweroff",
    "reg", "regedit", "sc", "wmic", "schtasks", "taskkill",
    "sudo", "su", "dd", "chmod", "chown", "takeown", "icacls", "cipher",
    "shred", "wipefs",
)

# shell 里用来串联多段的符号。切段后逐段校验首 token，防止 `a && rm -rf /`。
_SEPARATORS = ("&&", "||", "|", ";", "&")


def _first_token(segment: str) -> str:
    seg = segment.strip()
    if not seg:
        return ""
    # 去掉前导环境变量赋值：FOO=bar cmd  -> cmd
    parts = seg.split()
    for p in parts:
        if "=" in p and not p.startswith(("\"", "'")) and p.split("=", 1)[0].isidentifier():
            continue
        return p.strip("\"'")
    return ""


def command_segments(cmd: str) -> list[str]:
    out = [cmd]
    for sep in _SEPARATORS:
        nxt: list[str] = []
        for piece in out:
            nxt.extend(piece.split(sep))
        out = nxt
    return [s for s in (x.strip() for x in out) if s]


@dataclass
class Boundaries:
    """行为边界。全部有默认值，spec.boundaries 可逐项覆盖。"""

    workspace_root: Path
    max_steps: int = 1000
    max_wall_seconds: float = 3600.0
    max_total_attempts: int = 200
    allowed_commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = DEFAULT_DENIED
    require_approval: bool = False
    dry_run: bool = False
    extra_allowed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).resolve()
        if self.max_steps < 1:
            raise ValueError("max_steps 必须 ≥1")
        if self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds 必须 >0")
        if self.max_total_attempts < 1:
            raise ValueError("max_total_attempts 必须 ≥1")

    @classmethod
    def from_dict(cls, raw: dict[str, Any], workspace_root: Path) -> "Boundaries":
        raw = dict(raw or {})
        kwargs: dict[str, Any] = {"workspace_root": workspace_root}
        for key in (
            "max_steps",
            "max_wall_seconds",
            "max_total_attempts",
            "require_approval",
            "dry_run",
        ):
            if key in raw and raw[key] is not None:
                kwargs[key] = raw[key]
        if raw.get("allowed_commands"):
            kwargs["allowed_commands"] = tuple(str(x) for x in raw["allowed_commands"])
        if raw.get("denied_commands") is not None:
            # 显式给空列表 = 关闭内置拒绝表；未给 = 保留默认。
            kwargs["denied_commands"] = tuple(str(x) for x in raw["denied_commands"])
        if raw.get("extra_allowed_paths"):
            kwargs["extra_allowed_paths"] = tuple(
                str(x) for x in raw["extra_allowed_paths"]
            )
        return cls(**kwargs)


class Policy:
    """边界的执行者。所有检查都是「先判后做」，不做运行中改写。"""

    def __init__(self, boundaries: Boundaries) -> None:
        self.b = boundaries

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def resolve_path(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.b.workspace_root / p
        return p.expanduser().resolve()

    def is_inside(self, path: Path) -> bool:
        target = os.path.normcase(str(path))
        roots = [self.b.workspace_root] + [
            Path(x).resolve() for x in self.b.extra_allowed_paths
        ]
        for root in roots:
            r = os.path.normcase(str(root))
            if target == r or target.startswith(r.rstrip(os.sep) + os.sep):
                return True
        return False

    def check_path(self, path: str | os.PathLike[str], *, purpose: str = "写入") -> Path:
        resolved = self.resolve_path(path)
        if not self.is_inside(resolved):
            raise PolicyViolation(
                f"{purpose}路径越界：{resolved} 不在工作区根 {self.b.workspace_root} 内"
            )
        return resolved

    def check_paths(self, paths: Iterable[str], *, purpose: str = "写入") -> list[Path]:
        return [self.check_path(p, purpose=purpose) for p in paths]

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #
    def check_command(self, cmd: str) -> None:
        if not cmd.strip():
            raise PolicyViolation("空命令")
        denied = {d.lower() for d in self.b.denied_commands}
        allowed = tuple(a.lower() for a in self.b.allowed_commands)

        for seg in command_segments(cmd):
            token = _first_token(seg)
            if not token:
                continue
            name = os.path.basename(token).lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name in denied:
                raise PolicyViolation(f"命令被拒绝（危险名单）：{name}｜片段：{seg}")
            if allowed and not any(fnmatch.fnmatch(name, pat) for pat in allowed):
                raise PolicyViolation(
                    f"命令不在白名单内：{name}｜允许：{', '.join(allowed)}"
                )

    # ------------------------------------------------------------------ #
    # 预算
    # ------------------------------------------------------------------ #
    def check_budget(self, state: RunState, elapsed_s: float) -> None:
        c = state.counters
        # 用 >=：max_steps=N 的含义是「本轮最多再执行 N 步」，检查发生在执行之前。
        if c.get("steps_executed", 0) >= self.b.max_steps:
            state.boundaries_hit.append("max_steps")
            raise BudgetExceeded(f"步数达上限：{c['steps_executed']} >= {self.b.max_steps}")
        if elapsed_s > self.b.max_wall_seconds:
            state.boundaries_hit.append("max_wall_seconds")
            raise BudgetExceeded(
                f"墙钟超限：{elapsed_s:.1f}s > {self.b.max_wall_seconds}s"
            )
        if c.get("total_attempts", 0) >= self.b.max_total_attempts:
            state.boundaries_hit.append("max_total_attempts")
            raise BudgetExceeded(
                f"累计尝试达上限：{c['total_attempts']} >= {self.b.max_total_attempts}"
            )

    def describe(self) -> dict[str, Any]:
        return {
            "workspace_root": str(self.b.workspace_root),
            "max_steps": self.b.max_steps,
            "max_wall_seconds": self.b.max_wall_seconds,
            "max_total_attempts": self.b.max_total_attempts,
            "allowed_commands": list(self.b.allowed_commands),
            "denied_commands": list(self.b.denied_commands),
            "require_approval": self.b.require_approval,
            "dry_run": self.b.dry_run,
        }
