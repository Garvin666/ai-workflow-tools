"""[自研工具] total-agent

自研标识：Garvin666 自研工具 total-agent
名称：total-agent
用途：零依赖的多步骤任务总代理运行时 —— DAG 编排、行为边界、崩溃恢复、人工确认、结构化结果。
适用场景：长时多步骤实验/构建/数据处理流程的自主推进与断点续跑。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent（预留 https://github.com/Garvin666/ai-workflow-tools）

最小用法：
    from total_agent import TaskSpec, RunStore, Policy, Boundaries, Runtime

    spec = TaskSpec.from_dict({...})
    store = RunStore("./run-001")
    policy = Policy(Boundaries(workspace_root="."))
    state = Runtime(spec, store, policy).run()
"""

from __future__ import annotations

__version__ = "1.0.0"

from .errors import (
    ApprovalRejected,
    ApprovalRequired,
    BudgetExceeded,
    ExecutionError,
    ExecutionTimeout,
    IllegalTransition,
    NonZeroExit,
    PlanError,
    PolicyViolation,
    StateError,
    TotalAgentError,
    VerificationError,
)
from .executors import (
    DryRunExecutor,
    ExecResult,
    LLMExecutor,
    MockLLMExecutor,
    PythonExecutor,
    ShellExecutor,
    build_executor,
)
from .human import Approval, ApprovalQueue
from .model import (
    RunState,
    RunStatus,
    Step,
    StepState,
    StepStatus,
    TaskSpec,
    transition,
    transition_run,
)
from .planner import pick_next, ready_ids, topo_order, validate
from .policy import Boundaries, Policy
from .report import build_report, derive_next_actions, render_markdown
from .runtime import Runtime
from .store import RunStore

__all__ = [
    "__version__",
    # 模型
    "TaskSpec",
    "Step",
    "StepState",
    "StepStatus",
    "RunState",
    "RunStatus",
    "transition",
    "transition_run",
    # 编排
    "topo_order",
    "ready_ids",
    "pick_next",
    "validate",
    # 边界
    "Boundaries",
    "Policy",
    # 存储与运行时
    "RunStore",
    "Runtime",
    # 执行器
    "ExecResult",
    "ShellExecutor",
    "PythonExecutor",
    "DryRunExecutor",
    "LLMExecutor",
    "MockLLMExecutor",
    "build_executor",
    # 人工
    "Approval",
    "ApprovalQueue",
    # 报告
    "build_report",
    "render_markdown",
    "derive_next_actions",
    # 异常
    "TotalAgentError",
    "PlanError",
    "PolicyViolation",
    "IllegalTransition",
    "ExecutionError",
    "ExecutionTimeout",
    "NonZeroExit",
    "VerificationError",
    "ApprovalRequired",
    "ApprovalRejected",
    "StateError",
    "BudgetExceeded",
]
