"""当前原生分析流程的运行参数, 不创建外部资源."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MIN_ANALYSIS_TASKS = 2
MAX_ANALYSIS_PERSPECTIVES = 3


@dataclass(frozen=True, kw_only=True)
class AnalysisOptions:
    """固定批次、原生图调用限制、取证与模型传输配置.

    Attributes:
        max_tasks: 单批固定两个或三个独立探索方向.
        max_parallel: SDK task 的并发上限.
        max_worker_calls: 每个探索图的模型节点调用上限; 0 不附加限制.
        max_orchestrator_calls: 主图的模型节点调用上限; 0 不附加限制.
        max_integrator_calls: 整合图的模型节点调用上限; 0 不附加限制.
        output_dir: 独立运行目录的父目录.
        worker_observation_tools: 开放每个探索角色的原图裁剪与区域统计.
        max_measurements: 每个角色的不同取证操作上限; 缓存命中不重复计数.
        max_request_retries: 暂时性传输错误的额外请求次数.
        request_timeout_seconds: 每次网络请求尝试的超时秒数.
        stream_model_responses: 使用完整接收并校验的流式响应.
        max_output_tokens: 阶段字段缺省时的通用输出预算.
        outline_max_output_tokens: 主角色输出预算; 未设置时使用通用值.
        integration_max_output_tokens: 整合角色输出预算; 未设置时使用通用值.
        worker_max_output_tokens: 探索角色输出预算; 未设置时使用通用值.
    """

    max_tasks: int = MAX_ANALYSIS_PERSPECTIVES
    # 批次容量与并发上限独立: 降低并发不会减少冻结方向, SDK 可串行执行同一批.
    max_parallel: int = 3
    max_worker_calls: int = 0
    max_orchestrator_calls: int = 0
    max_integrator_calls: int = 0
    output_dir: Path | None = None
    worker_observation_tools: bool = False
    max_measurements: int = 8
    max_request_retries: int = 2
    request_timeout_seconds: int = 120
    stream_model_responses: bool = True
    max_output_tokens: int = 16384
    outline_max_output_tokens: int | None = None
    integration_max_output_tokens: int | None = None
    worker_max_output_tokens: int | None = None
    # CLI 通用覆盖仍优先于 YAML 阶段值, 只记录显式输入来源.
    _output_token_sources: tuple[tuple[str, str, int], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        """在调用模型或操作文件前拒绝无效配置."""
        self._validate_counts()
        self._validate_stage_outputs()
        if self.max_tasks not in {MIN_ANALYSIS_TASKS, MAX_ANALYSIS_PERSPECTIVES}:
            msg = "Native analysis requires exactly two or three perspectives"
            raise ValueError(msg)
        for name in ("worker_observation_tools", "stream_model_responses"):
            if not isinstance(getattr(self, name), bool):
                msg = f"{name} must be a boolean"
                raise TypeError(msg)

    def _validate_counts(self) -> None:
        """图限制和重试允许零, 其余容量必须为正整数."""
        positive = ("max_tasks", "max_parallel", "max_measurements", "request_timeout_seconds", "max_output_tokens")
        nonnegative = ("max_worker_calls", "max_orchestrator_calls", "max_integrator_calls", "max_request_retries")
        for names, minimum in ((positive, 1), (nonnegative, 0)):
            for name in names:
                value = getattr(self, name)
                # bool 在 Python 中也是 int, 需先排除, 防止开关被误当作次数或 token 预算.
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                    msg = f"{name} must be an integer >= {minimum}"
                    raise ValueError(msg)

    def _validate_stage_outputs(self) -> None:
        """阶段覆盖只接受正整数或空值, 保留通用回退语义."""
        for name in ("outline_max_output_tokens", "integration_max_output_tokens", "worker_max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                msg = f"{name} must be a positive integer or null"
                raise ValueError(msg)
