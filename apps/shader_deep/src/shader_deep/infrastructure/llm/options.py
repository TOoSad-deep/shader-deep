"""模型传输与阶段输出预算的最小只读配置."""

from dataclasses import dataclass
from typing import Literal, Protocol

AnalysisPhase = Literal["outline", "integration", "worker"]


# 传输层只依赖这些只读字段, 不需要导入工作流会话或持有完整 AnalysisOptions.
class ModelOptions(Protocol):
    """传输层只读取超时、流式开关与输出预算."""

    @property
    def request_timeout_seconds(self) -> int:
        """单次网络尝试的超时秒数."""
        ...

    @property
    def stream_model_responses(self) -> bool:
        """是否完整接收流式响应."""
        ...

    @property
    def max_output_tokens(self) -> int:
        """本次请求输出预算."""
        ...


class RoleOptions(ModelOptions, Protocol):
    """阶段输出值与显式配置来源."""

    @property
    def outline_max_output_tokens(self) -> int | None:
        """主角色输出预算."""
        ...

    @property
    def integration_max_output_tokens(self) -> int | None:
        """整合角色输出预算."""
        ...

    @property
    def worker_max_output_tokens(self) -> int | None:
        """探索角色输出预算."""
        ...

    @property
    def _output_token_sources(self) -> tuple[tuple[str, str, int], ...]:
        """显式输入的字段、来源与原始值."""
        ...


@dataclass(frozen=True)
class ResolvedModelOptions:
    """阶段输出预算已解析的传输配置."""

    request_timeout_seconds: int
    stream_model_responses: bool
    max_output_tokens: int


def resolve_role_options(options: RoleOptions, phase: AnalysisPhase) -> ResolvedModelOptions:
    """解析角色输出预算, 保持 CLI 通用覆盖优先于 YAML 阶段值.

    Args:
        options: 已校验的配置和显式来源.
        phase: 主角色、整合或探索阶段.

    Returns:
        当前阶段的最小传输配置.

    Raises:
        ValueError: 阶段名称不受支持.
    """
    if phase not in ("outline", "integration", "worker"):
        msg = f"Unknown analysis phase: {phase}"
        raise ValueError(msg)
    stage = f"{phase}_max_output_tokens"
    # 原值仍匹配才相信来源标签: dataclasses.replace 改值后, 旧标签不能继续决定优先级.
    sources = {name: origin for name, origin, value in options._output_token_sources if getattr(options, name) == value}
    stage_value = getattr(options, stage)
    value = options.max_output_tokens
    # 阶段 CLI > 通用 CLI > 显式阶段值 > 通用值; 缺省阶段值已经回退到通用配置.
    if sources.get(stage) == "cli" or (sources.get("max_output_tokens") != "cli" and stage_value is not None):
        value = stage_value
    return ResolvedModelOptions(options.request_timeout_seconds, options.stream_model_responses, value)
