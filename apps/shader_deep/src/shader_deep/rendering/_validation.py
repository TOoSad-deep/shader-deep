"""单帧渲染和常驻预览共用的输入校验, 不依赖浏览器."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from shader_deep.rendering.models import ParameterSpec

if TYPE_CHECKING:
    from collections.abc import Mapping


def validate_render_input(glsl_code: str, width: int, height: int, time: float) -> None:
    """在启动浏览器前校验源码、尺寸和时间的基本格式."""
    # 渲染器可以脱离 GenerationOptions 单独使用, 所以保留自身入口校验.
    # 这里只检查通用参数, 具体 GLSL 语法和显卡尺寸上限交给浏览器检查.
    if not isinstance(glsl_code, str) or not glsl_code.strip():
        msg = "glsl_code must be a non-empty string"
        raise ValueError(msg)
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"{name} must be a positive integer"
            raise ValueError(msg)
    if isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time):
        msg = "time must be a finite number of seconds"
        raise ValueError(msg)


def validate_uniform_values(uniforms: dict[str, float]) -> None:
    """校验 uniform 标识符及有限数值, 保护固定的时间和分辨率."""
    # 这里只接收自定义标量 float; 固定时间和分辨率不能被参数字典覆盖.
    for name, value in uniforms.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*", name) or name in {"iTime", "iResolution"}:
            msg = f"Invalid custom uniform name: {name}"
            raise ValueError(msg)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            msg = f"Uniform {name} must be a finite number"
            raise ValueError(msg)


def copy_parameter_specs(parameters: Mapping[str, ParameterSpec]) -> dict[str, ParameterSpec]:
    """校验并复制范围/初值, 将不完整配置统一报告为 ValueError."""
    try:
        specs = {name: ParameterSpec(min=spec["min"], max=spec["max"], initial=spec["initial"]) for name, spec in parameters.items()}
    except KeyError as exc:
        msg = f"Parameter configuration requires min/max/initial; missing {exc.args[0]}"
        raise ValueError(msg) from exc
    for field in ("min", "max", "initial"):
        validate_uniform_values({name: spec[field] for name, spec in specs.items()})
    for name, spec in specs.items():
        if not spec["min"] <= spec["initial"] <= spec["max"]:
            msg = f"Invalid parameter range or initial value: {name}"
            raise ValueError(msg)
    return specs
