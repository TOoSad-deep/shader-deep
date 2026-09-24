"""由程序管理的分析预算、执行记录与运行结果."""

from __future__ import annotations

from dataclasses import dataclass, field
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
        max_worker_calls: 每个子任务的模型调用上限, 包含修正报告的调用; 0 表示不限次数.
        max_main_calls: 主分析 Agent 在规划和综合阶段共享的调用上限; 0 表示不限次数.
        output_dir: 独立运行目录的父目录.
        max_measurements: 协调器执行不同图像测量操作的次数上限.
        max_request_retries: 每次逻辑模型调用因暂时性错误可增加的请求次数.
        request_timeout_seconds: 每次网络请求尝试的超时设置, 单位为秒.
        stream_model_responses: 以流式接收模型输出, 完整接收后再提交工具调用.
        max_output_tokens: 阶段字段缺省时的通用输出 token 预算.
        outline_max_output_tokens: 初稿阶段输出预算; 未设置时使用通用值.
        integration_max_output_tokens: 整合阶段输出预算; 未设置时使用通用值.
        worker_max_output_tokens: 探索子任务输出预算; 未设置时使用通用值.
        max_comparison_targets: 单组显式比较目标数上限.
        library_page_chars: 目录和工作索引单页字符预算.
        max_repeated_no_progress: 主任务连续同错同内容的停止阈值; 0 表示关闭.
        max_context_tokens: 应用估算的完整请求预算, 包含输出预留, 不代表提供方真实窗口.
        request_image_tokens: 每张图像的保守预算估算, 需按提供方调整.
        request_token_margin: 请求估算余量.
        max_integration_calls: 每个比较或发现阶段及初稿阶段的模型调用上限, 包含修复.
        max_integration_packages: 本轮整合阶段次数上限, 包含发现、比较、刷新、复核和结束.
        integration_no_progress: 整合阶段重复同错无进展的停止阈值.
        integration_history_tokens: 选材时为决定及局部修复历史预留的估算 token.
    """

    max_tasks: int = 6
    max_parallel: int = 3
    max_worker_calls: int = 0
    max_main_calls: int = 0
    output_dir: Path | None = None
    max_measurements: int = 8
    max_request_retries: int = 2
    request_timeout_seconds: int = 120
    stream_model_responses: bool = True
    max_output_tokens: int = 16384
    outline_max_output_tokens: int | None = None
    integration_max_output_tokens: int | None = None
    worker_max_output_tokens: int | None = None
    max_comparison_targets: int = 12
    library_page_chars: int = 8000
    # 保存显式输入来源与原值, 使 CLI 通用覆盖优先于 YAML 阶段值.
    _output_token_sources: tuple[tuple[str, str, int], ...] = field(default=(), repr=False, compare=False)
    max_repeated_no_progress: int = 0
    max_context_tokens: int = 262144
    request_image_tokens: int = 4096
    request_token_margin: int = 2048
    max_integration_calls: int = 12
    max_integration_packages: int = 24
    integration_no_progress: int = 3
    integration_history_tokens: int = 16384

    def __post_init__(self) -> None:
        """在调用模型或操作文件前拒绝无效预算."""
        for name in (
            "max_tasks",
            "max_parallel",
            "max_measurements",
            "request_timeout_seconds",
            "max_output_tokens",
            "max_comparison_targets",
            "library_page_chars",
            "max_context_tokens",
            "request_image_tokens",
            "request_token_margin",
            "max_integration_calls",
            "max_integration_packages",
            "integration_no_progress",
            "integration_history_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                msg = f"{name} must be a positive integer"
                raise ValueError(msg)
        self._validate_stage_outputs()
        for name in ("max_main_calls", "max_worker_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                msg = f"{name} must be a nonnegative integer; 0 means unlimited"
                raise ValueError(msg)
        if isinstance(self.max_repeated_no_progress, bool) or not isinstance(self.max_repeated_no_progress, int) or self.max_repeated_no_progress < 0:
            msg = "max_repeated_no_progress must be a nonnegative integer; 0 disables the guard"
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

    def _validate_stage_outputs(self) -> None:
        """可选阶段值只接受正整数, 不把 null 转换成实验默认值."""
        for name in ("outline_max_output_tokens", "integration_max_output_tokens", "worker_max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                msg = f"{name} must be a positive integer or null"
                raise ValueError(msg)


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


class AnalysisNoProgressError(RuntimeError):
    """显式启用的重复无进展检查终止当前主任务."""
