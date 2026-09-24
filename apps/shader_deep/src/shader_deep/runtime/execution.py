"""由程序管理的分析预算、执行记录与运行结果."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MIN_ANALYSIS_TASKS = 2


@dataclass(frozen=True, kw_only=True)
class RequestAttempt:
    """一次网络请求尝试的记录, 不保存凭据或服务响应正文."""

    model_call: int
    attempt: int
    elapsed_seconds: float
    error_type: str | None = None
    cause_types: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ToolFeedback:
    """简短的工具校验或恢复反馈, 不记录模型的完整参数正文."""

    model_call: int
    tool: str
    message: str
    category: str = "unspecified"
    repair_scope: str | None = None


@dataclass(kw_only=True)
class AnalysisExecution:
    """执行器维护的可变状态; 错误和计数均由程序填写."""

    status: Literal["pending", "running", "completed", "failed", "stopped"] = "pending"
    # 此计数约束角色的思考/纠错轮数; request_attempts 额外记录各轮内部的网络恢复.
    model_calls: int = 0
    error: str | None = None
    request_attempts: tuple[RequestAttempt, ...] = ()
    tool_feedback: tuple[ToolFeedback, ...] = ()
    format_repair_calls: int = 0
    # 作用域绑定阶段或工作身份, 循环重建和暂缓接续均不重置.
    format_repair_scopes: dict[str, int] = field(default_factory=dict)
    # 成功提交解决该身份此前的失败反馈; 后续同轮新错误仍需正常修复.
    format_repair_resolved_through: dict[str, int] = field(default_factory=dict)


class AnalysisLimitError(RuntimeError):
    """某个角色已耗尽应用层的逻辑模型调用预算."""


class AnalysisNoProgressError(RuntimeError):
    """显式启用的重复无进展检查终止当前主任务."""
