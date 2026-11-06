"""[自研工具] total-agent · 持久化与恢复

自研标识：Garvin666 自研工具 total-agent / 用途：状态落盘与崩溃恢复 / 适用场景：长时任务断点续跑。
仓库链接：待推送。

两条铁律：
1. journal.jsonl 是真相源，state.json 是缓存 —— 恢复时以前者校正后者。
2. 写状态必须原子（tmp + os.replace），否则崩溃会留下半截 JSON。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import StateError
from .model import RunState, StepState, StepStatus, TaskSpec, now_iso, transition


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class RunStore:
    """一个 run_dir 对应一次运行。所有落盘只经这里。"""

    SPEC = "spec.json"
    STATE = "state.json"
    JOURNAL = "journal.jsonl"
    REPORT = "report.json"
    APPROVALS = "approvals"
    ARTIFACTS = "artifacts"

    def __init__(self, run_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(run_dir).resolve()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def initialize(self, spec: TaskSpec) -> None:
        if self.spec_path.exists():
            raise StateError(f"{self.dir} 已有运行记录，不能重复 initialize")
        for sub in (self.APPROVALS, self.ARTIFACTS):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.spec_path, spec.to_dict())
        self.append_event({"event": "run_created", "run_id": spec.run_id, "goal": spec.goal})

    def exists(self) -> bool:
        return self.spec_path.exists() and self.state_path.exists()

    @property
    def spec_path(self) -> Path:
        return self.dir / self.SPEC

    @property
    def state_path(self) -> Path:
        return self.dir / self.STATE

    @property
    def journal_path(self) -> Path:
        return self.dir / self.JOURNAL

    @property
    def report_path(self) -> Path:
        return self.dir / self.REPORT

    @property
    def approvals_dir(self) -> Path:
        return self.dir / self.APPROVALS

    @property
    def artifacts_dir(self) -> Path:
        return self.dir / self.ARTIFACTS

    def load_spec(self) -> TaskSpec:
        if not self.spec_path.exists():
            raise StateError(f"{self.spec_path} 不存在：该目录没有运行记录")
        return TaskSpec.from_dict(_read_json(self.spec_path))

    def load_state(self) -> RunState:
        if not self.state_path.exists():
            raise StateError(f"{self.state_path} 不存在")
        try:
            return RunState.from_dict(_read_json(self.state_path))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise StateError(f"state.json 损坏：{exc}") from exc

    def save_state(self, state: RunState) -> None:
        state.updated_at = now_iso()
        _atomic_write_json(self.state_path, state.to_dict())

    def write_report(self, payload: dict[str, Any]) -> Path:
        _atomic_write_json(self.report_path, payload)
        return self.report_path

    # ------------------------------------------------------------------ #
    # journal
    # ------------------------------------------------------------------ #
    def append_event(self, event: dict[str, Any]) -> None:
        record = {"ts": now_iso(), **event}
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_events(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.journal_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # 崩溃可能留下半行。它必然在尾部，丢弃并记录，不静默。
                    events.append(
                        {
                            "event": "journal_corrupt_line",
                            "lineno": lineno,
                            "raw_len": len(line),
                        }
                    )
        return events

    def finished_ok_keys(self) -> set[str]:
        keys: set[str] = set()
        for e in self.read_events():
            if e.get("event") == "step_finished" and e.get("ok"):
                key = e.get("key") or e.get("step_id")
                if key:
                    keys.add(key)
        return keys

    def consumed_keys(self, event_names: Iterable[str] = ("step_started",)) -> set[str]:
        names = set(event_names)
        out: set[str] = set()
        for e in self.read_events():
            if e.get("event") in names:
                key = e.get("key") or e.get("step_id")
                if key:
                    out.add(key)
        return out

    # ------------------------------------------------------------------ #
    # 中断修复
    # ------------------------------------------------------------------ #
    def repair(self, state: RunState) -> list[str]:
        """用 journal 校正 state。返回人类可读的修复说明（空列表 = 无需修复）。

        规则（见 DESIGN §7.2）：
        - running 且 journal 有 step_finished(ok)  -> succeeded（状态文件落后了，不重跑）
        - running 且 journal 无 step_finished       -> interrupted（真被打断，待重跑）
        """
        notes: list[str] = []
        done = self.finished_ok_keys()

        for sid, st in state.steps.items():
            if st.status is not StepStatus.RUNNING:
                continue
            key = st.evidence.get("idempotency_key") if st.evidence else None
            key = key or f"step:{sid}"
            if sid in done or key in done:
                transition(st, StepStatus.SUCCEEDED)
                st.ended_at = st.ended_at or now_iso()
                st.reason = "recovered_from_journal"
                notes.append(f"{sid}: running → succeeded（journal 显示已完成，跳过重跑）")
            else:
                transition(st, StepStatus.INTERRUPTED)
                st.reason = "interrupted"
                notes.append(f"{sid}: running → interrupted（将被重新排队）")
        return notes

    # ------------------------------------------------------------------ #
    # 产物
    # ------------------------------------------------------------------ #
    def artifact_path(self, relative: str) -> Path:
        p = (self.artifacts_dir / relative).resolve()
        if not str(p).startswith(str(self.artifacts_dir)):
            raise StateError(f"产物路径逃出 artifacts 目录：{relative}")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_artifact_text(self, relative: str, text: str) -> Path:
        p = self.artifact_path(relative)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        return p
