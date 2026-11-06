"""[自研工具] total-agent · 错误分类

自研标识：本文件属 Garvin666 自研工具 total-agent，非第三方复用。
名称：total-agent
用途：零依赖的多步骤任务总代理运行时（DAG 编排 + 状态持久化 + 人工门 + 结构化结果）。
适用场景：长时多步实验流程的自主推进与断点续跑。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/tree/main/tools/total-agent

错误分类决定后续动作：可重试 / 需人工 / 需终止。分类写在这里，不散落在捕获点。
"""

from __future__ import annotations


class TotalAgentError(Exception):
    """所有框架异常的基类。"""

    code: str = "total_agent_error"
    retryable: bool = False


class PlanError(TotalAgentError):
    """计划本身不合法：环、重复 id、未知依赖、空计划。"""

    code = "plan_error"


class PolicyViolation(TotalAgentError):
    """越界：路径出工作区、命令被拒、预算超限。不可重试 —— 重试只会再撞一次。"""

    code = "policy_violation"


class IllegalTransition(TotalAgentError):
    """状态机拒绝的迁移。属实现 bug 或状态文件被外部改写。"""

    code = "illegal_transition"


class ExecutionError(TotalAgentError):
    """执行失败的基类。"""

    code = "execution_error"
    retryable = True


class ExecutionTimeout(ExecutionError):
    code = "timeout"


class NonZeroExit(ExecutionError):
    code = "nonzero_exit"


class VerificationError(ExecutionError):
    code = "verify_failed"


class ApprovalRequired(TotalAgentError):
    """步骤需要人工确认。不是错误，是暂停信号。"""

    code = "approval_required"


class ApprovalRejected(TotalAgentError):
    """人工驳回。该步取消，下游跳过。"""

    code = "approval_rejected"


class StateError(TotalAgentError):
    """状态文件损坏或与 journal 不一致到无法修复。"""

    code = "state_error"


class BudgetExceeded(TotalAgentError):
    """步数/墙钟/累计尝试超限。触发运行终止。"""

    code = "budget_exceeded"


def error_payload(exc: BaseException) -> dict:
    """把异常压成可序列化的结构，写入 state 与报告。"""
    return {
        "code": getattr(exc, "code", "unexpected_error"),
        "message": str(exc),
        "retryable": bool(getattr(exc, "retryable", False)),
        "type": type(exc).__name__,
    }
