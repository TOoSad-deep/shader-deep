"""加载评审 YAML 与显式覆盖, 保留配置文件相对路径语义."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from shader_deep.infrastructure.configuration import read_yaml_mapping
from shader_deep.workflows.review.options import ReviewOptions

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "resources" / "review.yaml"


def apply_review_overrides(options: ReviewOptions, overrides: dict[str, object]) -> ReviewOptions:
    """应用显式参数并复用运行选项的严格校验.

    Args:
        options: 已加载的评审配置.
        overrides: 调用方显式指定的公开字段.

    Returns:
        已通过校验的新配置.

    Raises:
        ValueError: 出现未知字段或无效预算.
    """
    allowed = {item.name for item in fields(ReviewOptions)}
    if set(overrides) - allowed:
        msg = "Unknown review option override"
        raise ValueError(msg)
    return replace(options, **overrides)


def _output_path(value: object, selected: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = "review output_dir must be a nonblank string or null"
        raise ValueError(msg)
    path = Path(value)
    return path if path.is_absolute() else selected.resolve().parent / path


def load_review_options(path: Path | None = None) -> ReviewOptions:
    """读取包内或指定评审配置.

    Args:
        path: 显式 YAML 路径, 省略时读取包内 review.yaml.

    Returns:
        YAML 覆盖后的有效评审选项.

    Raises:
        OSError: 显式配置无法读取.
        ValueError: 字段或参数无效.
    """
    selected = path if path is not None else DEFAULT_CONFIG
    if path is None and not selected.exists():
        return ReviewOptions()
    payload = read_yaml_mapping(selected, label="review")
    values: dict[str, object] = {}
    for name, value in payload.items():
        if not isinstance(name, str):
            msg = "review option names must be strings"
            raise TypeError(msg)
        values[name] = value
    if "output_dir" in values:
        values["output_dir"] = _output_path(values["output_dir"], selected)
    return apply_review_overrides(ReviewOptions(), values)
