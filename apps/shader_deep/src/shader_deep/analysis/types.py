"""由程序管理的分析预算、执行记录与运行结果."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.schemas import BlackboardState, ResultRecord

MIN_ANALYSIS_TASKS = 2


@dataclass(frozen=True, kw_only=True)
class AnalysisOptions:
    """分析预算覆盖追加任务、失败任务和无效模型响应.

    Attributes:
        max_tasks: 独立任务总数上限, 包含失败任务和追加任务.
        max_parallel: 同时执行的分析工作线程数上限.
        max_worker_calls: 每个子任务的模型调用上限, 包含修正报告的调用.
        max_main_calls: 主分析 Agent 在规划和综合阶段共享的调用上限.
        output_dir: 独立运行目录的父目录.
        max_measurements: 协调器执行不同图像测量操作的次数上限.
        max_request_retries: 每次逻辑模型调用因暂时性错误可增加的请求次数.
        request_timeout_seconds: 每次网络请求尝试的超时设置, 单位为秒.
        stream_model_responses: 以流式接收模型输出, 完整接收后再提交工具调用.
        max_output_tokens: 每次请求交给模型服务的输出 token 预算.
    """

    max_tasks: int = 6
    max_parallel: int = 3
    max_worker_calls: int = 3
    max_main_calls: int = 8
    output_dir: Path | None = None
    max_measurements: int = 8
    max_request_retries: int = 2
    request_timeout_seconds: int = 120
    stream_model_responses: bool = True
    max_output_tokens: int = 16384

    def __post_init__(self) -> None:
        """在调用模型或操作文件前拒绝无效预算."""
        for name in (
            "max_tasks",
            "max_parallel",
            "max_worker_calls",
            "max_main_calls",
            "max_measurements",
            "request_timeout_seconds",
            "max_output_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                msg = f"{name} must be a positive integer"
                raise ValueError(msg)
        if self.max_tasks < MIN_ANALYSIS_TASKS:
            msg = "Multi-view analysis requires a budget of at least two tasks"
            raise ValueError(msg)
        if isinstance(self.max_request_retries, bool) or not isinstance(self.max_request_retries, int) or self.max_request_retries < 0:
            msg = "max_request_retries must be a nonnegative integer"
            raise ValueError(msg)
        if not isinstance(self.stream_model_responses, bool):
            msg = "stream_model_responses must be a boolean"
            raise TypeError(msg)


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


@dataclass(kw_only=True)
class AnalysisExecution:
    """执行器维护的可变状态; 错误和计数均由程序填写."""

    status: Literal["pending", "running", "completed", "failed", "stopped"] = "pending"
    # 此计数约束角色的思考/纠错轮数; request_attempts 额外记录各轮内部的网络恢复.
    model_calls: int = 0
    error: str | None = None
    request_attempts: tuple[RequestAttempt, ...] = ()
    tool_feedback: tuple[ToolFeedback, ...] = ()


@dataclass(frozen=True, kw_only=True)
class AnalysisOutcome:
    """完整或中断的分析结果, 包含运行保留的所有业务记录."""

    state: BlackboardState
    task_id: str
    summary_result: ResultRecord | None
    run_dir: Path
    stop_reason: str


class AnalysisLimitError(RuntimeError):
    """某个角色已耗尽应用层的逻辑模型调用预算."""
