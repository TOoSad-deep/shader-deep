"""从 YAML 加载分析运行参数, 沿用 AnalysisOptions 的类型与预算校验."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Literal

from shader_deep.infrastructure.configuration import read_yaml_mapping
from shader_deep.workflows.options import AnalysisOptions

AnalysisPhase = Literal["outline", "integration", "worker"]
OUTPUT_FIELDS = ("max_output_tokens", "outline_max_output_tokens", "integration_max_output_tokens", "worker_max_output_tokens")

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "resources" / "analysis.yaml"


def _read_values(path: Path) -> dict[str, object]:
    """只接收安全 YAML 的顶层参数映射, 拒绝未知字段."""
    payload = read_yaml_mapping(path, label="analysis")
    allowed = {field.name for field in fields(AnalysisOptions) if not field.name.startswith("_")}
    values: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or key not in allowed:
            msg = f"Unknown analysis option in {path}: {key}"
            raise ValueError(msg)
        values[key] = value
    return values


def _output_path(value: object, path: Path) -> Path | None:
    """YAML 中的相对输出目录以配置文件所在目录为基准."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = f"output_dir must be a nonblank string or null: {path}"
        raise ValueError(msg)
    directory = Path(value)
    return directory if directory.is_absolute() else path.resolve().parent / directory


def load_analysis_options(path: Path | None = None) -> AnalysisOptions:
    """读取分析配置; 默认加载包内 resources/analysis.yaml, 不依赖启动目录.

    Args:
        path: YAML 路径; 未提供时使用模块内配置, 该文件缺失时回退内置默认值.

    Returns:
        已通过预算校验的选项; 缺省字段沿用 AnalysisOptions 的默认值.

    Raises:
        OSError: 显式指定的文件不存在或文件不可读取.
        ValueError: YAML 格式、字段名、字段类型或参数范围无效.
        TypeError: 参数类型不符合 AnalysisOptions 契约.
    """
    selected = path if path is not None else DEFAULT_CONFIG
    if path is None and not selected.exists():
        return AnalysisOptions()
    values = _read_values(selected)
    if "output_dir" in values:
        values["output_dir"] = _output_path(values["output_dir"], selected)
    # replace 复用现有 dataclass 校验, 不对字符串数字、布尔值等做隐式类型转换.
    return _with_sources(AnalysisOptions(), values, "yaml")


def _with_sources(options: AnalysisOptions, values: dict[str, object], source: str) -> AnalysisOptions:
    """仅标注实际设置的输出预算, 不将缺省回退伪装成配置输入."""
    sources = {name: (origin, value) for name, origin, value in options._output_token_sources}
    for name in OUTPUT_FIELDS:
        if name in values:
            value = values[name]
            if isinstance(value, int) and not isinstance(value, bool):
                sources[name] = (source, value)
            else:
                sources.pop(name, None)
    metadata = tuple((name, origin, value) for name, (origin, value) in sources.items())
    return replace(options, **values, _output_token_sources=metadata)


def apply_analysis_overrides(options: AnalysisOptions, overrides: dict[str, object]) -> AnalysisOptions:
    """应用显式 CLI 覆盖, 保留通用 CLI 对全部阶段的优先级.

    Args:
        options: 已加载的 YAML 或内置选项.
        overrides: 仅包含用户显式设置的公开选项.

    Returns:
        保存输入来源且已校验的选项副本.

    Raises:
        ValueError: 覆盖字段不属于公开配置或预算不合法.
    """
    allowed = {item.name for item in fields(AnalysisOptions) if not item.name.startswith("_")}
    if set(overrides) - allowed:
        msg = "Unknown or internal analysis option override"
        raise ValueError(msg)
    return _with_sources(options, overrides, "cli")


def resolve_phase_options(options: AnalysisOptions, phase: AnalysisPhase) -> tuple[AnalysisOptions, str]:
    """统一解析阶段预算, 供请求参数、预算检查及运行记录共同使用.

    Args:
        options: 保留原始阶段配置和来源的完整运行选项.
        phase: 初稿、整合或探索子任务阶段.

    Returns:
        max_output_tokens 已解析的选项副本及来源; 输入选项保持不变.

    Raises:
        ValueError: 阶段名称不合法.
    """
    if phase not in ("outline", "integration", "worker"):
        msg = f"Unknown analysis phase: {phase}"
        raise ValueError(msg)
    stage = f"{phase}_max_output_tokens"
    sources = {name: origin for name, origin, value in options._output_token_sources if getattr(options, name) == value}
    stage_value = getattr(options, stage)
    if sources.get(stage) == "cli":
        value, source = stage_value, "stage_cli"
    elif sources.get("max_output_tokens") == "cli":
        value, source = options.max_output_tokens, "cli"
    elif stage_value is not None:
        value, source = stage_value, "stage_yaml" if sources.get(stage) == "yaml" else "stage_options"
    else:
        value = options.max_output_tokens
        source = sources.get("max_output_tokens", "default" if value == AnalysisOptions().max_output_tokens else "options")
    return replace(options, max_output_tokens=value), source
