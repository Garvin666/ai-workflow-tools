"""[自研工具] total-agent · 结构化结果

自研标识：Garvin666 自研工具 total-agent / 用途：把运行状态压成交付可用结论 / 适用场景：任务收尾汇报。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent。

五个必填面与需求一一对应：最终产出 / 已完成步骤 / 未完成项 / 异常原因 / 后续建议。
next_actions 由状态与错误码推导，不是套话模板 —— 每条都能指向一个具体动作。
"""

from __future__ import annotations

from typing import Any, Iterable

from .human import ApprovalQueue
from .model import (
    TERMINAL_STEP_STATUSES,
    RunState,
    RunStatus,
    StepStatus,
    TaskSpec,
    now_iso,
)

ADVICE: dict[str, str] = {
    "timeout": "调大该步 timeout_s，或把它拆成两个更短的步骤",
    "nonzero_exit": "看 stderr 尾部定位失败点；修好命令后 resume，已完成的步骤不会重跑",
    "verify_failed": "执行退出了 0 但验证不通过 —— 先核对 verify 判据与产出约定是否一致",
    "policy_violation": "边界拒绝：确认目标路径在工作区内，或把该命令加入 boundaries.allowed_commands",
    "exception": "步骤代码抛异常：看 stderr 的异常类型与栈顶",
    "not_found": "找不到执行目标：检查 command / target 拼写，或 python 执行器是否已注册",
    "os_error": "系统调用失败：检查路径是否存在、权限是否足够",
    "llm_error": "模型调用失败：检查凭据与配额（凭据只经环境变量传入）",
}


def _actions_for_error(step_id: str, code: str, message: str) -> str:
    tip = ADVICE.get(code, "查看该步 stderr 与 journal 记录后决定重试或改计划")
    snippet = (message or "").strip().replace("\n", " ")[:120]
    tail = f"｜原始信息：{snippet}" if snippet else ""
    return f"步骤 {step_id} 因 {code} 失败 —— {tip}{tail}"


def build_report(
    state: RunState,
    spec: TaskSpec,
    store: Any | None = None,
    *,
    events: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    step_map = spec.step_map
    completed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for step in spec.steps:
        st = state.steps.get(step.id)
        if st is None:
            continue
        ev = st.evidence or {}
        if st.status is StepStatus.SUCCEEDED:
            completed.append(
                {
                    "id": step.id,
                    "action": step.action or step.command or step.target,
                    "attempts": st.attempts,
                    "duration_s": round(st.duration_s, 3),
                    "verification": ev.get("verification", "none"),
                    "exit_code": ev.get("exit_code"),
                }
            )
            outputs.append(
                {
                    "step": step.id,
                    "action": step.action or step.command or step.target,
                    "artifacts": list(step.artifacts),
                    "summary": (ev.get("stdout_tail") or "").strip()[-300:],
                    "verification": ev.get("verification", "none"),
                }
            )
        else:
            pending.append(
                {
                    "id": step.id,
                    "action": step.action or step.command or step.target,
                    "status": st.status.value,
                    "reason": st.reason or _default_reason(st.status),
                    "attempts": st.attempts,
                }
            )
        if st.error:
            anomalies.append(
                {
                    "step": step.id,
                    "code": st.error.get("code", "unknown"),
                    "message": st.error.get("message", ""),
                    "attempts": st.attempts,
                    "retryable": bool(st.error.get("retryable", False)),
                }
            )

    approvals: list[dict[str, Any]] = []
    if store is not None:
        try:
            approvals = [a.to_dict() for a in ApprovalQueue(store.approvals_dir).pending()]
        except OSError:
            approvals = []

    report = {
        "run_id": state.run_id,
        "goal": state.goal,
        "status": state.status.value,
        "generated_at": now_iso(),
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "counters": dict(state.counters),
        "final_outputs": outputs,
        "completed_steps": completed,
        "pending_steps": pending,
        "anomalies": anomalies,
        "next_actions": derive_next_actions(state, spec, approvals),
        "pending_approvals": approvals,
        "boundaries_hit": list(state.boundaries_hit),
        "pause_reason": state.pause_reason,
        "abort_reason": state.abort_reason,
    }
    return report


def _default_reason(status: StepStatus) -> str:
    return {
        StepStatus.PENDING: "依赖未满足",
        StepStatus.READY: "等待调度（上次中断）",
        StepStatus.RUNNING: "执行中（若为终态报告，说明进程异常退出）",
        StepStatus.BLOCKED: "等待人工确认",
        StepStatus.SKIPPED: "上游未成功，已跳过",
        StepStatus.CANCELLED: "运行被终止",
        StepStatus.FAILED: "失败",
        StepStatus.INTERRUPTED: "中断",
        StepStatus.SUCCEEDED: "已完成",
    }.get(status, "")


def derive_next_actions(
    state: RunState,
    spec: TaskSpec,
    approvals: list[dict[str, Any]] | None = None,
) -> list[str]:
    actions: list[str] = []
    approvals = approvals or []

    if state.status is RunStatus.SUCCEEDED:
        return ["全部步骤已完成，无需后续动作；产出见 final_outputs"]

    if state.status is RunStatus.PAUSED or approvals:
        ids = [a["step_id"] for a in approvals] or [
            s.id for s in state.steps.values() if s.status is StepStatus.BLOCKED
        ]
        actions.append(
            "运行暂停，等待人工确认：" + ", ".join(ids)
            + "。执行 `total-agent approve <run_dir> <step_id>` 或 `reject`，再 `resume`"
        )

    for st in state.steps.values():
        if st.status is StepStatus.FAILED and st.error:
            actions.append(
                _actions_for_error(
                    st.id, st.error.get("code", "unknown"), st.error.get("message", "")
                )
            )

    skipped = [s for s in state.steps.values() if s.status is StepStatus.SKIPPED]
    if skipped:
        actions.append(
            "被跳过的步骤：" + ", ".join(s.id for s in skipped)
            + " —— 上游修复后 resume 会自动带起，不需要单独重置"
        )

    if state.status is RunStatus.ABORTED:
        if "max_steps" in state.boundaries_hit:
            actions.append("命中步数上限：调大 boundaries.max_steps，或把计划拆成多次运行")
        if "max_wall_seconds" in state.boundaries_hit:
            actions.append("命中墙钟上限：调大 boundaries.max_wall_seconds，或把长步骤拆分")
        if "max_total_attempts" in state.boundaries_hit:
            actions.append("命中累计尝试上限：先排查反复失败的那一步，不要盲目调大")
        if not state.boundaries_hit:
            actions.append(f"运行中止：{state.abort_reason or '未知原因'}")

    if not actions:
        actions.append("运行未正常收口，检查 journal.jsonl 尾部与 state.json 是否一致")
    return actions


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# 总代理运行报告 · {report['run_id']}")
    add("")
    add(f"- **目标**：{report['goal']}")
    add(f"- **状态**：`{report['status']}`")
    add(f"- **起止**：{report['started_at']} → {report['ended_at'] or '（未收口）'}")
    c = report.get("counters", {})
    add(
        f"- **计数**：执行 {c.get('steps_executed', 0)} 步 / 累计尝试 {c.get('total_attempts', 0)} 次"
        f" / resume {c.get('resumes', 0)} 次"
    )
    add("")

    add("## 1. 最终产出")
    if report["final_outputs"]:
        add("")
        add("| 步骤 | 动作 | 产物 | 验证 |")
        add("| --- | --- | --- | --- |")
        for o in report["final_outputs"]:
            arts = "<br>".join(o["artifacts"]) if o["artifacts"] else "（无文件产物）"
            add(f"| {o['step']} | {o['action']} | {arts} | {o['verification']} |")
    else:
        add("")
        add("（无）")
    add("")

    add("## 2. 已完成步骤")
    if report["completed_steps"]:
        add("")
        add("| 步骤 | 动作 | 尝试 | 耗时(s) | 验证 |")
        add("| --- | --- | --- | --- | --- |")
        for s in report["completed_steps"]:
            add(
                f"| {s['id']} | {s['action']} | {s['attempts']} | {s['duration_s']} | {s['verification']} |"
            )
    else:
        add("")
        add("（无）")
    add("")

    add("## 3. 未完成项")
    if report["pending_steps"]:
        add("")
        add("| 步骤 | 状态 | 原因 | 尝试 |")
        add("| --- | --- | --- | --- |")
        for p in report["pending_steps"]:
            add(f"| {p['id']} | {p['status']} | {p['reason']} | {p['attempts']} |")
    else:
        add("")
        add("（无）")
    add("")

    add("## 4. 异常原因")
    if report["anomalies"]:
        add("")
        add("| 步骤 | 错误码 | 可重试 | 信息 |")
        add("| --- | --- | --- | --- |")
        for a in report["anomalies"]:
            msg = (a["message"] or "").replace("|", "\\|").replace("\n", " ")[:200]
            add(f"| {a['step']} | {a['code']} | {'是' if a['retryable'] else '否'} | {msg} |")
    else:
        add("")
        add("（无）")
    add("")

    add("## 5. 后续处理建议")
    add("")
    for i, act in enumerate(report["next_actions"], 1):
        add(f"{i}. {act}")
    add("")
    return "\n".join(lines)
