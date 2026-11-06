"""[自研工具] total-agent · 运行时主循环

自研标识：Garvin666 自研工具 total-agent / 用途：调度-执行-验证-重试-暂停-终止 / 适用场景：自主推进多步任务。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent。

一轮循环做五件事：查预算 → 看是否已收口 → 消化审批 → 取一个就绪步 → 执行它。
没有 ready 且没有 blocked，就是收口（或死锁）。这个判据只有一处，不在别处重复。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import planner
from .errors import (
    BudgetExceeded,
    IllegalTransition,
    PolicyViolation,
    StateError,
    error_payload,
)
from .executors import (
    CallableStep,
    DryRunExecutor,
    ExecResult,
    LLMExecutor,
    build_executor,
    run_shell,
)
from .human import ApprovalQueue
from .model import (
    TERMINAL_STEP_STATUSES,
    RunState,
    RunStatus,
    Step,
    StepStatus,
    TaskSpec,
    now_iso,
    transition,
    transition_run,
)
from .policy import Policy
from .store import RunStore

# 这些失败重试有意义；policy/plan 类的重试只会再撞一次，一律不重试。
RETRYABLE_KINDS = frozenset(
    {"timeout", "nonzero_exit", "verify_failed", "exception", "llm_error", "os_error"}
)


class Runtime:
    def __init__(
        self,
        spec: TaskSpec,
        store: RunStore,
        policy: Policy,
        *,
        python_registry: dict[str, CallableStep] | None = None,
        llm: LLMExecutor | None = None,
        dry_run: bool | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.spec = spec
        self.store = store
        self.policy = policy
        self.approvals = ApprovalQueue(store.approvals_dir)
        self.python_registry = python_registry or {}
        self.llm = llm
        self.dry_run = policy.b.dry_run if dry_run is None else dry_run
        self.on_event = on_event
        self._events: list[dict[str, Any]] | None = None
        self.step_map = spec.step_map

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def run(self) -> RunState:
        planner.validate(self.spec.steps)
        self.store.initialize(self.spec)
        state = RunState.create(self.spec)
        self.store.save_state(state)
        self._log({"event": "run_started", "run_id": state.run_id})
        return self._loop(state)

    def resume(self) -> RunState:
        if not self.store.exists():
            raise StateError(f"{self.store.dir} 不是一次已有运行")
        self.spec = self.store.load_spec()
        planner.validate(self.spec.steps)
        self.step_map = self.spec.step_map

        state = self.store.load_state()
        if state.is_terminal():
            raise StateError(f"运行已处于终态 {state.status.value}，不能 resume")

        notes = self.store.repair(state)
        for sid, st in state.steps.items():
            if st.status is StepStatus.INTERRUPTED:
                transition(st, StepStatus.READY)
                st.reason = "resumed_after_interrupt"
        state.counters["resumes"] = state.counters.get("resumes", 0) + 1
        if state.status is RunStatus.PAUSED:
            transition_run(state, RunStatus.RUNNING, "resumed")
        self._log(
            {
                "event": "resumed",
                "run_id": state.run_id,
                "resume_no": state.counters["resumes"],
                "repairs": notes,
            }
        )
        self.store.save_state(state)
        return self._loop(state)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _loop(self, state: RunState) -> RunState:
        wall_start = time.monotonic()

        while True:
            try:
                self.policy.check_budget(state, time.monotonic() - wall_start)
            except BudgetExceeded as exc:
                return self._abort(state, str(exc))

            if state.all_terminal():
                return self._finalize(state, time.monotonic() - wall_start)

            self._apply_approvals(state)

            if state.status is RunStatus.PAUSED and self._has_blocked(state):
                self.store.save_state(state)
                return state

            step = planner.pick_next(self.spec.steps, state.steps)
            if step is None:
                if self._has_blocked(state):
                    transition_run(state, RunStatus.PAUSED, "waiting_approval")
                    self.store.save_state(state)
                    return state
                return self._finalize(state, time.monotonic() - wall_start)

            self._execute(step, state)
            if state.status is RunStatus.PAUSED:
                self.store.save_state(state)
                return state

    # ------------------------------------------------------------------ #
    # 单步执行
    # ------------------------------------------------------------------ #
    def _execute(self, step: Step, state: RunState) -> None:
        st = state.steps[step.id]

        # 1) 审批门
        if (self.policy.b.require_approval or step.requires_approval) and not self._is_approved(step.id):
            self._block(
                step,
                state,
                step.question or f"步骤「{step.action or step.id}」需要人工确认后执行",
            )
            return

        # 2) 副作用可疑：上次执行过但没确认收尾，重放可能重复改外部状态
        if step.side_effects and self._has_unfinished_attempt(step):
            self._block(
                step,
                state,
                (
                    f"步骤「{step.action or step.id}」被标记为有副作用，且上次执行未确认结束"
                    f"（幂等键 {step.key}）。请确认外部状态后决定是否重放。"
                ),
            )
            return

        # 3) 进入执行态（pending 要先就绪；状态机不允许 pending 直接 running）
        if st.status is StepStatus.PENDING:
            transition(st, StepStatus.READY)
        transition(st, StepStatus.RUNNING)
        st.started_at = now_iso()
        st.attempts += 1
        state.counters["steps_executed"] = state.counters.get("steps_executed", 0) + 1
        state.counters["total_attempts"] = state.counters.get("total_attempts", 0) + 1
        self._log(
            {
                "event": "step_started",
                "step_id": step.id,
                "key": step.key,
                "attempt": st.attempts,
            }
        )
        self.store.save_state(state)

        # 4) 边界检查：越界属不可重试，直接收口
        try:
            if step.executor == "shell":
                self.policy.check_command(step.command)
            if step.artifacts:
                self.policy.check_paths(step.artifacts, purpose="产出")
        except PolicyViolation as exc:
            self._fail_without_retry(step, state, error_payload(exc))
            return

        # 5) 真正执行
        result = self._invoke(step)

        if result.needs_info:
            self._block(step, state, result.needs_info)
            return

        if result.ok and step.verify and not self.dry_run:
            result = self._verify(step, result)

        if result.ok:
            transition(st, StepStatus.SUCCEEDED)
            st.ended_at = now_iso()
            st.duration_s = result.duration_s
            st.error = None
            evidence = result.to_evidence()
            evidence["verification"] = (
                "dry_run"
                if result.simulated
                else ("command" if step.verify else "none")
            )
            evidence["idempotency_key"] = step.key
            st.evidence = evidence
            st.reason = ""
            self._log(
                {
                    "event": "step_finished",
                    "step_id": step.id,
                    "key": step.key,
                    "ok": True,
                    "duration_s": round(result.duration_s, 3),
                }
            )
            self.store.save_state(state)
            return

        self._handle_failure(step, state, result)

    def _invoke(self, step: Step) -> ExecResult:
        if self.dry_run:
            return DryRunExecutor().run(step, self.policy.b.workspace_root, step.timeout_s)
        executor = build_executor(
            step.executor,
            python_registry=self.python_registry,
            llm=self.llm,
        )
        return executor.run(step, self.policy.b.workspace_root, step.timeout_s)

    def _verify(self, step: Step, prior: ExecResult) -> ExecResult:
        check = run_shell(
            step.verify, cwd=self.policy.b.workspace_root, timeout_s=step.timeout_s
        )
        if check.ok:
            merged = ExecResult(
                ok=True,
                exit_code=prior.exit_code,
                stdout=prior.stdout,
                stderr=prior.stderr,
                duration_s=prior.duration_s + check.duration_s,
                simulated=prior.simulated,
            )
            return merged
        return ExecResult(
            ok=False,
            exit_code=check.exit_code,
            stdout=prior.stdout,
            stderr=(prior.stderr + "\n[verify] " + check.stderr).strip(),
            duration_s=prior.duration_s + check.duration_s,
            error_kind="verify_failed" if check.error_kind != "timeout" else "timeout",
        )

    def _handle_failure(self, step: Step, state: RunState, result: ExecResult) -> None:
        st = state.steps[step.id]
        kind = result.error_kind or "execution_error"
        payload = {
            "code": kind,
            "message": (result.stderr or result.stdout or "").strip()[-500:],
            "retryable": kind in RETRYABLE_KINDS,
            "type": "ExecResult",
            "exit_code": result.exit_code,
        }
        st.error = payload
        st.duration_s = result.duration_s

        if payload["retryable"] and st.attempts < step.max_attempts:
            transition(st, StepStatus.READY)
            note = (
                f"第 {st.attempts} 次失败（{kind}）：{payload['message'][:120]}；"
                f"将进行第 {st.attempts + 1} 次尝试"
            )
            st.retry_notes.append(note)
            self._log(
                {
                    "event": "retry",
                    "step_id": step.id,
                    "key": step.key,
                    "attempt": st.attempts,
                    "note": note,
                }
            )
            self.store.save_state(state)
            return

        transition(st, StepStatus.FAILED)
        st.ended_at = now_iso()
        st.reason = payload["code"]
        self._log(
            {
                "event": "step_finished",
                "step_id": step.id,
                "key": step.key,
                "ok": False,
                "error_kind": kind,
                "attempts": st.attempts,
            }
        )
        self._propagate_skip(state, step.id, "dependency_failed")
        self.store.save_state(state)

    def _fail_without_retry(
        self, step: Step, state: RunState, payload: dict[str, Any]
    ) -> None:
        st = state.steps[step.id]
        transition(st, StepStatus.FAILED)
        st.ended_at = now_iso()
        st.error = payload
        st.reason = payload["code"]
        self._log(
            {
                "event": "step_finished",
                "step_id": step.id,
                "key": step.key,
                "ok": False,
                "error_kind": payload["code"],
            }
        )
        self._propagate_skip(state, step.id, "dependency_failed")
        self.store.save_state(state)

    # ------------------------------------------------------------------ #
    # 阻塞 / 跳过 / 终止
    # ------------------------------------------------------------------ #
    def _block(self, step: Step, state: RunState, question: str) -> None:
        st = state.steps[step.id]
        if st.status is StepStatus.PENDING:
            # 迁移表里 pending 不能直接到 blocked —— 先就绪再挂起，保持单一路径
            transition(st, StepStatus.READY)
        if st.status in (StepStatus.READY, StepStatus.RUNNING):
            transition(st, StepStatus.BLOCKED)
        st.reason = "blocked: " + question[:180]
        self.approvals.request(step.id, question)
        self._log(
            {
                "event": "blocked",
                "step_id": step.id,
                "question": question,
            }
        )
        transition_run(state, RunStatus.PAUSED, f"waiting_approval:{step.id}")
        self.store.save_state(state)

    def _propagate_skip(self, state: RunState, step_id: str, reason: str) -> None:
        for sid in planner.downstream(self.spec.steps, step_id):
            st = state.steps[sid]
            if st.status in TERMINAL_STEP_STATUSES:
                continue
            try:
                transition(st, StepStatus.SKIPPED)
            except IllegalTransition:
                continue
            st.reason = reason
            self._log({"event": "step_skipped", "step_id": sid, "reason": reason})

    def _abort(self, state: RunState, reason: str) -> RunState:
        for st in state.steps.values():
            if st.status in TERMINAL_STEP_STATUSES:
                continue
            try:
                transition(st, StepStatus.CANCELLED)
            except IllegalTransition:
                pass
            st.reason = "run_aborted"
        transition_run(state, RunStatus.ABORTED, reason)
        self._log({"event": "aborted", "reason": reason})
        self.store.save_state(state)
        return state

    def _finalize(self, state: RunState, wall_s: float) -> RunState:
        failed = [s for s in state.steps.values() if s.status is StepStatus.FAILED]
        succeeded = [s for s in state.steps.values() if s.status is StepStatus.SUCCEEDED]
        unfinished = [
            s for s in state.steps.values() if s.status not in TERMINAL_STEP_STATUSES
        ]
        if failed:
            transition_run(
                state,
                RunStatus.FAILED,
                f"{len(failed)} 个步骤失败：" + ", ".join(s.id for s in failed),
            )
        elif not succeeded:
            # 全部被取消/跳过 —— 没有任何产出，不能报成功
            transition_run(
                state,
                RunStatus.FAILED,
                "没有步骤成功（全部被取消或跳过）",
            )
        elif unfinished:
            transition_run(
                state,
                RunStatus.ABORTED,
                "死锁：无就绪步骤但仍有未完成项 —— "
                + ", ".join(s.id for s in unfinished),
            )
        else:
            transition_run(state, RunStatus.SUCCEEDED)
        self._log(
            {
                "event": "run_finished",
                "status": state.status.value,
                "wall_s": round(wall_s, 3),
            }
        )
        self.store.save_state(state)
        return state

    # ------------------------------------------------------------------ #
    # 审批与 journal 辅助
    # ------------------------------------------------------------------ #
    def _apply_approvals(self, state: RunState) -> None:
        for sid, st in state.steps.items():
            if st.status is not StepStatus.BLOCKED:
                continue
            ap = self.approvals.get(sid)
            if ap is None or ap.is_pending:
                continue
            if ap.status == "approved":
                transition(st, StepStatus.READY)
                st.reason = "approved" + (f"：{ap.note}" if ap.note else "")
                self._log({"event": "approval_granted", "step_id": sid, "note": ap.note})
            else:
                transition(st, StepStatus.CANCELLED)
                st.reason = "approval_rejected" + (f"：{ap.note}" if ap.note else "")
                self._log({"event": "approval_rejected", "step_id": sid, "note": ap.note})
                self._propagate_skip(state, sid, "upstream_rejected")

    def _is_approved(self, step_id: str) -> bool:
        ap = self.approvals.get(step_id)
        return ap is not None and ap.status == "approved"

    def _has_blocked(self, state: RunState) -> bool:
        return any(st.status is StepStatus.BLOCKED for st in state.steps.values())

    def _has_unfinished_attempt(self, step: Step) -> bool:
        started = 0
        finished = 0
        for e in self._event_log():
            if (e.get("key") or e.get("step_id")) != step.key:
                continue
            if e.get("event") == "step_started":
                started += 1
            elif e.get("event") == "step_finished":
                finished += 1
        return started > finished

    def _event_log(self) -> list[dict[str, Any]]:
        if self._events is None:
            self._events = self.store.read_events()
        return self._events

    def _log(self, event: dict[str, Any]) -> None:
        self.store.append_event(event)
        if self._events is not None:
            self._events.append({"ts": now_iso(), **event})
        if self.on_event is not None:
            self.on_event(event)
