"""[自研工具] total-agent · 人工确认

自研标识：Garvin666 自研工具 total-agent / 用途：阻塞时的人工介入协议 / 适用场景：需人拍板的步骤。
仓库链接：待推送。

协议是文件：approvals/<step>.json。写请求 -> 人改文件（或用 CLI）-> resume 读结论。
刻意不接消息通道：谁来通知人不是编排内核的职责，接进来会把框架绑死在某个 IM 上。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import now_iso

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(step_id: str) -> str:
    cleaned = _SAFE.sub("_", step_id).strip("._-")
    return cleaned or "step"


@dataclass
class Approval:
    step_id: str
    status: str
    question: str
    created_at: str
    decided_at: str = ""
    note: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Approval":
        return cls(
            step_id=raw["step_id"],
            status=raw.get("status", PENDING),
            question=raw.get("question", ""),
            created_at=raw.get("created_at", ""),
            decided_at=raw.get("decided_at", ""),
            note=raw.get("note", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "question": self.question,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "note": self.note,
        }


class ApprovalQueue:
    def __init__(self, approvals_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(approvals_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, step_id: str) -> Path:
        return self.dir / f"{_safe_name(step_id)}.json"

    def get(self, step_id: str) -> Approval | None:
        p = self._path(step_id)
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            return Approval.from_dict(json.load(f))

    def request(self, step_id: str, question: str) -> Approval:
        existing = self.get(step_id)
        if existing is not None and existing.is_pending:
            return existing  # 不重复发请求，避免把人的待办清单刷爆
        ap = Approval(step_id=step_id, status=PENDING, question=question, created_at=now_iso())
        self._write(ap)
        return ap

    def decide(self, step_id: str, approved: bool, note: str = "") -> Approval:
        ap = self.get(step_id)
        if ap is None:
            raise KeyError(f"步骤 {step_id} 没有审批请求")
        if not ap.is_pending:
            raise ValueError(f"步骤 {step_id} 的审批已结案：{ap.status}")
        ap.status = APPROVED if approved else REJECTED
        ap.decided_at = now_iso()
        ap.note = note
        self._write(ap)
        return ap

    def pending(self) -> list[Approval]:
        out: list[Approval] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ap = Approval.from_dict(json.load(f))
            except (json.JSONDecodeError, KeyError):
                continue
            if ap.is_pending:
                out.append(ap)
        return out

    def _write(self, ap: Approval) -> None:
        target = self._path(ap.step_id)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(ap.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
