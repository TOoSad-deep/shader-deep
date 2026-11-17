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
    """单观察角色预算、传输参数及旧多角色配置占位.

    Attributes:
        max_model_calls: 主模型节点的有限调用预算.
        max_tasks: 弃用占位, 仅保留旧默认值 3.
        max_parallel: 弃用占位, 仅保留旧默认值 3.
        max_worker_calls: 弃用占位, 仅保留旧默认值 0.
        max_orchestrator_calls: 弃用占位, 仅保留旧默认值 0.
        max_integrator_calls: 弃用占位, 仅保留旧默认值 0.
        output_dir: 独立运行目录的父目录.
        worker_observation_tools: 弃用占位, 单观察角色只接受 false.
        max_measurements: 弃用占位, 仅保留旧默认值 8.
        max_request_retries: 暂时性传输错误的额外请求次数.
        request_timeout_seconds: 每次网络请求尝试的超时秒数.
        stream_model_responses: 使用完整接收并校验的流式响应.
        max_output_tokens: 单观察角色的输出 token 预算.
        outline_max_output_tokens: 弃用占位, 新执行只接受 null.
        integration_max_output_tokens: 弃用占位, 新执行只接受 null.
        worker_max_output_tokens: 弃用占位, 新执行只接受 null.
    """

    max_model_calls: int = 20
    max_tasks: int = MAX_ANALYSIS_PERSPECTIVES
    # 旧字段保持构造签名, 执行入口在 CLI 覆盖完成后检查兼容占位.
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

    _deprecated_option_sources: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        """在调用模型或操作文件前拒绝无效配置."""
        self._validate_counts()
        self._validate_stage_outputs()
        for name in ("worker_observation_tools", "stream_model_responses"):
            if not isinstance(getattr(self, name), bool):
                msg = f"{name} must be a boolean"
                raise TypeError(msg)

    def _validate_counts(self) -> None:
        """图限制和重试允许零, 其余容量必须为正整数."""
        positive = ("max_model_calls", "max_tasks", "max_parallel", "max_measurements", "request_timeout_seconds", "max_output_tokens")
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


LEGACY_OBSERVATION_DEFAULTS: dict[str, int | bool | None] = {
    "max_tasks": 3,
    "max_parallel": 3,
    "max_measurements": 8,
    "max_worker_calls": 0,
    "max_orchestrator_calls": 0,
    "max_integrator_calls": 0,
    "worker_observation_tools": False,
    "outline_max_output_tokens": None,
    "worker_max_output_tokens": None,
    "integration_max_output_tokens": None,
}


def validate_observation_options(options: AnalysisOptions) -> None:
    """在覆盖完成后拒绝不适用于单观察角色的旧配置.

    Args:
        options: 已合并显式覆盖的分析配置.

    Raises:
        ValueError: 旧参数含非兼容占位值.
    """
    invalid = [name for name, value in LEGACY_OBSERVATION_DEFAULTS.items() if getattr(options, name) != value]
    if invalid:
        msg = (
            "单 Agent 全图观察不支持这些旧配置的非默认值: "
            + ", ".join(invalid)
            + "; 请删除旧字段, 使用 max_model_calls 和 max_output_tokens. max_tasks=1 不是切换开关."
        )
        raise ValueError(msg)


def observation_option_diagnostics(options: AnalysisOptions) -> tuple[str, ...]:
    """生成弃用配置诊断, 不将占位值描述为实际角色数.

    Args:
        options: 已合并覆盖的配置.

    Returns:
        供 stderr 和运行记录使用的诊断文字.
    """
    if not options._deprecated_option_sources:
        return ()
    fields = ", ".join(options._deprecated_option_sources)
    return (f"弃用配置仅为兼容占位, 不控制本轮单 Agent: {fields}; 请移除旧字段.",)
