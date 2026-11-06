"""[自研工具] total-agent · DAG 编排

自研标识：Garvin666 自研工具 total-agent / 用途：依赖解析与调度顺序 / 适用场景：多步骤有依赖的任务。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent。

四件事：校验计划、算拓扑序、算就绪集、算下游闭包。
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping, Sequence

from .errors import PlanError
from .model import Step, StepState, StepStatus


def validate(steps: Sequence[Step]) -> None:
    """计划合法性。任一不满足都抛 PlanError —— 不在运行期才发现。"""
    if not steps:
        raise PlanError("计划为空：至少需要一个步骤")

    seen: set[str] = set()
    for s in steps:
        if s.id in seen:
            raise PlanError(f"重复的步骤 id：{s.id}")
        seen.add(s.id)

    for s in steps:
        for dep in s.depends_on:
            if dep == s.id:
                raise PlanError(f"步骤 {s.id} 依赖自身")
            if dep not in seen:
                raise PlanError(f"步骤 {s.id} 依赖不存在的步骤 {dep}")

    topo_order(steps)  # 有环则抛


def topo_order(steps: Sequence[Step]) -> list[str]:
    """Kahn 拓扑排序。同层按计划给定顺序保持稳定。"""
    index = {s.id: i for i, s in enumerate(steps)}
    indeg = {s.id: 0 for s in steps}
    children: dict[str, list[str]] = {s.id: [] for s in steps}

    for s in steps:
        for dep in s.depends_on:
            indeg[s.id] += 1
            children[dep].append(s.id)

    queue = deque(sorted((k for k, v in indeg.items() if v == 0), key=lambda k: index[k]))
    ordered: list[str] = []
    while queue:
        cur = queue.popleft()
        ordered.append(cur)
        ready_children: list[str] = []
        for child in children[cur]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready_children.append(child)
        for child in sorted(ready_children, key=lambda k: index[k]):
            queue.append(child)

    if len(ordered) != len(steps):
        stuck = [s.id for s in steps if s.id not in ordered]
        raise PlanError(f"计划存在循环依赖，涉及步骤：{', '.join(stuck)}")
    return ordered


def is_ready(step: Step, states: Mapping[str, StepState]) -> bool:
    """单步就绪判定：自身待跑，且全部依赖已成功。"""
    st = states.get(step.id)
    if st is None or st.status not in (StepStatus.PENDING, StepStatus.READY):
        return False
    for dep in step.depends_on:
        dep_state = states.get(dep)
        if dep_state is None or dep_state.status is not StepStatus.SUCCEEDED:
            return False
    return True


def ready_ids(steps: Sequence[Step], states: Mapping[str, StepState]) -> list[str]:
    return [s.id for s in steps if is_ready(s, states)]


def order_key(steps: Sequence[Step]) -> dict[str, int]:
    return {sid: i for i, sid in enumerate(topo_order(steps))}


def pick_next(steps: Sequence[Step], states: Mapping[str, StepState]) -> Step | None:
    """就绪集内按 (priority 降序, 拓扑序) 取一个。串行执行，不并发。"""
    ready = set(ready_ids(steps, states))
    if not ready:
        return None
    rank = order_key(steps)
    candidates = [s for s in steps if s.id in ready]
    candidates.sort(key=lambda s: (-s.priority, rank[s.id]))
    return candidates[0]


def downstream(steps: Sequence[Step], step_id: str) -> list[str]:
    """传递依赖该步的所有步骤（不含自身），按拓扑序返回。"""
    children: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.depends_on:
            children[dep].append(s.id)

    order = order_key(steps)
    seen: set[str] = set()
    stack = list(children.get(step_id, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return sorted(seen, key=lambda k: order.get(k, 10**9))


def blocked_by_unsatisfiable(
    steps: Sequence[Step], states: Mapping[str, StepState]
) -> list[str]:
    """找出「永远不可能就绪」的步骤：仍非终态，但依赖里已有失败/跳过/取消。

    用于死锁判定 —— 没有 ready、没有 running、也没有 blocked，但还有非终态步骤，
    这说明计划本身卡死了，必须终止而不是空转。
    """
    dead = {
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    }
    out: list[str] = []
    for s in steps:
        st = states.get(s.id)
        if st is None or st.status in (
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        ):
            continue
        for dep in s.depends_on:
            dep_state = states.get(dep)
            if dep_state is not None and dep_state.status in dead:
                out.append(s.id)
                break
    return out
