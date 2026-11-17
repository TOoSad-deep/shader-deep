"""加载规划 YAML 与显式覆盖, 保留配置文件相对路径语义."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from shader_deep.infrastructure.configuration import read_yaml_mapping
from shader_deep.workflows.planning.options import PlanningOptions

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "resources" / "planning.yaml"


def apply_planning_overrides(options: PlanningOptions, overrides: dict[str, object]) -> PlanningOptions:
    """应用显式参数并复用运行选项的严格校验.

    Args:
        options: 已加载的规划配置.
        overrides: 调用方显式指定的公开字段.

    Returns:
        已通过校验的新配置.

    Raises:
        ValueError: 出现未知字段或无效预算.
    """
    allowed = {item.name for item in fields(PlanningOptions)}
    if set(overrides) - allowed:
        msg = "Unknown planning option override"
        raise ValueError(msg)
    return replace(options, **overrides)


def _output_path(value: object, selected: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = "planning output_dir must be a nonblank string or null"
        raise ValueError(msg)
    path = Path(value)
    return path if path.is_absolute() else selected.resolve().parent / path


def load_planning_options(path: Path | None = None) -> PlanningOptions:
    """读取包内或指定规划配置.

    Args:
        path: 显式 YAML 路径, 省略时读取包内 planning.yaml.

    Returns:
        YAML 覆盖后的有效规划选项.

    Raises:
        OSError: 显式配置无法读取.
        ValueError: 字段或参数无效.
    """
    selected = path if path is not None else DEFAULT_CONFIG
    if path is None and not selected.exists():
        return PlanningOptions()
    payload = read_yaml_mapping(selected, label="planning")
    values: dict[str, object] = {}
    for name, value in payload.items():
        if not isinstance(name, str):
            msg = "planning option names must be strings"
            raise TypeError(msg)
        values[name] = value
    if "output_dir" in values:
        values["output_dir"] = _output_path(values["output_dir"], selected)
    return apply_planning_overrides(PlanningOptions(), values)
