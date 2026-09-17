"""从 YAML 加载分析运行参数, 沿用 AnalysisOptions 的类型与预算校验."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import yaml

from shader_deep.analysis.types import AnalysisOptions

DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.yaml")


def _read_values(path: Path) -> dict[str, object]:
    """只接收安全 YAML 的顶层参数映射, 拒绝未知字段."""
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"Invalid analysis YAML: {path}: {exc}"
        raise ValueError(msg) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        msg = f"Analysis YAML must contain a parameter mapping: {path}"
        raise TypeError(msg)
    allowed = {field.name for field in fields(AnalysisOptions)}
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
    """读取分析配置; 默认加载分析模块旁的 config.yaml, 不依赖启动目录.

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
    return replace(AnalysisOptions(), **values)
