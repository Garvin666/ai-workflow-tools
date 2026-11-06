"""[自研工具] total-agent · 数据模型与状态机

自研标识：Garvin666 自研工具 total-agent / 用途：多步骤任务编排与恢复 / 适用场景：长时实验流程。
仓库链接：待推送。

状态迁移表是唯一口径源；任何模块改状态都必须走 transition()，不允许直接赋值。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence

from .errors import IllegalTransition, PlanError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 步骤状态
# --------------------------------------------------------------------------- #


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    }
)

# 只列「合法」的迁出；不在表里的一律抛 IllegalTransition。
STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY, StepStatus.SKIPPED, StepStatus.CANCELLED}),
    StepStatus.READY: frozenset(
        {StepStatus.RUNNING, StepStatus.BLOCKED, StepStatus.SKIPPED, StepStatus.CANCELLED}
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.READY,        # 重试
            StepStatus.BLOCKED,      # 需人工
            StepStatus.INTERRUPTED,  # 崩溃恢复
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.BLOCKED: frozenset(
        {StepStatus.READY, StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED}
    ),
    StepStatus.INTERRUPTED: frozenset({StepStatus.READY, StepStatus.CANCELLED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABORTED}
)

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(
        {RunStatus.PAUSED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABORTED}
    ),
    RunStatus.PAUSED: frozenset(
        {RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABORTED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.ABORTED: frozenset(),
}


# --------------------------------------------------------------------------- #
# 计划对象
# --------------------------------------------------------------------------- #

VALID_EXECUTORS = ("shell", "python", "llm", "none")
VALID_TIERS = ("strong", "mid", "cheap", "default")


@dataclass
class Step:
    """计划中的一个步骤。字段与 spec JSON 一一对应。"""

    id: str
    action: str = ""
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    executor: str = "shell"
    command: str = ""
    target: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    verify: str = ""
    timeout_s: float = 120.0
    max_attempts: int = 1
    requires_approval: bool = False
    side_effects: bool = False
    artifacts: tuple[str, ...] = ()
    idempotency_key: str = ""
    model_tier: str = "default"
    question: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise PlanError("步骤缺少 id")
        if self.executor not in VALID_EXECUTORS:
            raise PlanError(f"步骤 {self.id} 的 executor 非法：{self.executor}")
        if self.model_tier not in VALID_TIERS:
            raise PlanError(f"步骤 {self.id} 的 model_tier 非法：{self.model_tier}")
        if self.executor == "shell" and not self.command:
            raise PlanError(f"步骤 {self.id} 声明 shell 执行但未给 command")
        if self.executor == "python" and not self.target:
            raise PlanError(f"步骤 {self.id} 声明 python 执行但未给 target")
        if self.max_attempts < 1:
            raise PlanError(f"步骤 {self.id} 的 max_attempts 必须 ≥1")
        if self.timeout_s <= 0:
            raise PlanError(f"步骤 {self.id} 的 timeout_s 必须 >0")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", f"step:{self.id}")

    @property
    def key(self) -> str:
        return self.idempotency_key

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Step":
        known = {
            "id",
            "action",
            "depends_on",
            "priority",
            "executor",
            "command",
            "target",
            "args",
            "verify",
            "timeout_s",
            "max_attempts",
            "requires_approval",
            "side_effects",
            "artifacts",
            "idempotency_key",
            "model_tier",
            "question",
        }
        kwargs = {k: v for k, v in raw.items() if k in known}
        if "depends_on" in kwargs:
            kwargs["depends_on"] = tuple(kwargs["depends_on"] or ())
        if "artifacts" in kwargs:
            kwargs["artifacts"] = tuple(kwargs["artifacts"] or ())
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "executor": self.executor,
            "command": self.command,
            "target": self.target,
            "args": self.args,
            "verify": self.verify,
            "timeout_s": self.timeout_s,
            "max_attempts": self.max_attempts,
            "requires_approval": self.requires_approval,
            "side_effects": self.side_effects,
            "artifacts": list(self.artifacts),
            "idempotency_key": self.idempotency_key,
            "model_tier": self.model_tier,
            "question": self.question,
        }


@dataclass
class TaskSpec:
    """一次运行的计划快照。首跑写入 run_dir/spec.json，之后 resume 只读它。"""

    goal: str
    steps: list[Step]
    inputs: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    boundaries: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"

    @property
    def step_map(self) -> dict[str, Step]:
        return {s.id: s for s in self.steps}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskSpec":
        if not isinstance(raw, dict):
            raise PlanError("spec 顶层必须是对象")
        goal = raw.get("goal") or ""
        if not goal:
            raise PlanError("spec 缺少 goal")
        raw_steps = raw.get("steps")
        if not raw_steps or not isinstance(raw_steps, list):
            raise PlanError("spec 的 steps 必须是非空列表")
        steps = [Step.from_dict(s) for s in raw_steps]
        return cls(
            goal=goal,
            steps=steps,
            inputs=raw.get("inputs") or {},
            constraints=list(raw.get("constraints") or []),
            acceptance=list(raw.get("acceptance") or []),
            boundaries=raw.get("boundaries") or {},
            run_id=raw.get("run_id") or "",
            name=raw.get("name") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "goal": self.goal,
            "inputs": self.inputs,
            "constraints": self.constraints,
            "acceptance": self.acceptance,
            "boundaries": self.boundaries,
            "steps": [s.to_dict() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# 运行期状态
# --------------------------------------------------------------------------- #


@dataclass
class StepState:
    id: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0
    error: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    reason: str = ""
    retry_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepState":
        status = raw.get("status", "pending")
        return cls(
            id=raw["id"],
            status=StepStatus(status),
            attempts=int(raw.get("attempts", 0)),
            started_at=raw.get("started_at", ""),
            ended_at=raw.get("ended_at", ""),
            duration_s=float(raw.get("duration_s", 0.0)),
            error=raw.get("error"),
            evidence=raw.get("evidence"),
            reason=raw.get("reason", ""),
            retry_notes=list(raw.get("retry_notes") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
            "evidence": self.evidence,
            "reason": self.reason,
            "retry_notes": self.retry_notes,
        }


@dataclass
class RunState:
    run_id: str
    goal: str
    status: RunStatus = RunStatus.RUNNING
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str = field(default_factory=now_iso)
    ended_at: str = ""
    steps: dict[str, StepState] = field(default_factory=dict)
    counters: dict[str, int] = field(
        default_factory=lambda: {"steps_executed": 0, "total_attempts": 0, "resumes": 0}
    )
    pause_reason: str = ""
    abort_reason: str = ""
    boundaries_hit: list[str] = field(default_factory=list)
    plan_version: int = 1

    @classmethod
    def create(cls, spec: TaskSpec) -> "RunState":
        return cls(
            run_id=spec.run_id,
            goal=spec.goal,
            steps={s.id: StepState(id=s.id) for s in spec.steps},
        )

    def all_terminal(self) -> bool:
        return all(st.status in TERMINAL_STEP_STATUSES for st in self.steps.values())

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
            "counters": self.counters,
            "pause_reason": self.pause_reason,
            "abort_reason": self.abort_reason,
            "boundaries_hit": self.boundaries_hit,
            "plan_version": self.plan_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunState":
        return cls(
            run_id=raw["run_id"],
            goal=raw.get("goal", ""),
            status=RunStatus(raw.get("status", "running")),
            created_at=raw.get("created_at", now_iso()),
            updated_at=raw.get("updated_at", now_iso()),
            started_at=raw.get("started_at", now_iso()),
            ended_at=raw.get("ended_at", ""),
            steps={k: StepState.from_dict(v) for k, v in (raw.get("steps") or {}).items()},
            counters=raw.get("counters")
            or {"steps_executed": 0, "total_attempts": 0, "resumes": 0},
            pause_reason=raw.get("pause_reason", ""),
            abort_reason=raw.get("abort_reason", ""),
            boundaries_hit=list(raw.get("boundaries_hit") or []),
            plan_version=int(raw.get("plan_version", 1)),
        )


# --------------------------------------------------------------------------- #
# 迁移守卫
# --------------------------------------------------------------------------- #


def transition(step_state: StepState, target: StepStatus) -> None:
    """步骤状态迁移。非法迁移抛 IllegalTransition —— 不静默放过。"""
    if step_state.status == target:
        return
    allowed = STEP_TRANSITIONS.get(step_state.status, frozenset())
    if target not in allowed:
        raise IllegalTransition(
            f"步骤 {step_state.id} 不允许 {step_state.status.value} → {target.value}"
        )
    step_state.status = target


def transition_run(state: RunState, target: RunStatus, reason: str = "") -> None:
    if state.status == target:
        return
    allowed = RUN_TRANSITIONS.get(state.status, frozenset())
    if target not in allowed:
        raise IllegalTransition(f"运行不允许 {state.status.value} → {target.value}")
    state.status = target
    if target in TERMINAL_RUN_STATUSES:
        state.ended_at = now_iso()
    if reason:
        if target == RunStatus.PAUSED:
            state.pause_reason = reason
        elif target == RunStatus.ABORTED:
            state.abort_reason = reason


def summarize_statuses(state: RunState) -> dict[str, int]:
    counts: dict[str, int] = {}
    for st in state.steps.values():
        counts[st.status.value] = counts.get(st.status.value, 0) + 1
    return counts
