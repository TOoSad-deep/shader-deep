"""生成候选的外置连续参数合同与独立 GLSL 导出."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shader_deep.domain.scene_observations import ObservationId  # noqa: TC001  # Pydantic 运行时读取字段约束.

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

UniformValue = float | tuple[float, ...]
UniformInput = UniformValue | list[float]
UniformType = Literal["float", "vec2", "vec3", "vec4"]
_DIMENSIONS: dict[UniformType, int] = {"float": 1, "vec2": 2, "vec3": 3, "vec4": 4}
_FLOAT32_MAX = 3.4028234663852886e38
_Float = Annotated[float, Field(strict=True, ge=-_FLOAT32_MAX, le=_FLOAT32_MAX, allow_inf_nan=False)]
_Value = _Float | tuple[_Float, ...]
_RESERVED = frozenset({"iResolution", "iTime"})
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_COMMENTS = re.compile(r"//[^\r\n]*|/\*.*?\*/", re.DOTALL)
_DIRECTIVES = re.compile(r"^[ \t]*#(?:[^\r\n]*?\\\r?\n)*[^\r\n]*", re.MULTILINE)
_UNIFORM = re.compile(r"\buniform\b")
_NON_STANDALONE = re.compile(r"[^;}\s]\s*\buniform\b")
_DECLARATION = re.compile(rf"(?P<keyword>\buniform)\s+(?:(?:lowp|mediump|highp)\s+)?(?P<type>float|vec[234])\s+(?P<name>{_IDENTIFIER})\s*(?P<end>;)")


def _name(value: str) -> str:
    if value in _RESERVED or value.startswith("gl_") or "__" in value:
        msg = f"Reserved uniform name: {value}"
        raise ValueError(msg)
    return value


class ParameterSpec(BaseModel):
    """用逐分量的硬边界描述一个标量或向量 uniform.

    Attributes:
        name: GLSL 中与声明一致的标识符.
        type: 允许的标量或向量类型.
        default: 首次渲染值.
        min: 允许的最小值, 向量按分量指定.
        max: 允许的最大值, 向量按分量指定.
        target_ref: 可选上游 E/F/R 目标标识.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Annotated[str, Field(strict=True, pattern=rf"^{_IDENTIFIER}$"), AfterValidator(_name)]
    type: UniformType
    default: _Value
    min: _Value
    max: _Value
    target_ref: ObservationId | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        lower, upper = self._components(self.min), self._components(self.max)
        if any(low > high for low, high in zip(lower, upper, strict=True)):
            msg = f"Uniform {self.name} has a minimum greater than its maximum"
            raise ValueError(msg)
        self.validate_value(self.default)
        return self

    def _components(self, value: UniformInput) -> tuple[float, ...]:
        dimension = _DIMENSIONS[self.type]
        if dimension == 1 and not isinstance(value, (list, tuple)):
            return (value,)
        if dimension > 1 and isinstance(value, (list, tuple)) and len(value) == dimension:
            return tuple(value)
        msg = f"Uniform {self.name} requires a {self.type} value"
        raise ValueError(msg)

    def validate_value(self, value: UniformInput) -> UniformValue:
        """按 GLSL 形状和本参数上下界校验并规范化一次赋值.

        Args:
            value: 原始标量或向量赋值.

        Returns:
            浮点标量或存为元组的浮点向量.

        Raises:
            ValueError: 赋值形状不符或分量超出硬边界.
        """
        components = self._components(value)
        for part, low, high in zip(components, self._components(self.min), self._components(self.max), strict=True):
            if isinstance(part, bool) or not isinstance(part, (int, float)) or not low <= part <= high:
                msg = f"Uniform {self.name} must stay within its declared component bounds"
                raise ValueError(msg)
        values = tuple(float(part) for part in components)
        return values[0] if self.type == "float" else values


def resolve_uniforms(specs: Sequence[ParameterSpec], values: Mapping[str, UniformInput] | None = None) -> dict[str, UniformValue]:
    """填充默认值并拒绝重复声明或未声明的参数.

    Args:
        specs: 已校验的参数合同.
        values: 可选的部分或全部实际赋值.

    Returns:
        按声明顺序组织的完整参数映射.

    Raises:
        ValueError: 声明有重名、值越界或提供了合同之外的名称.
    """
    overrides = {} if values is None else values
    names = {spec.name for spec in specs}
    if len(names) != len(specs):
        msg = "ParameterSpec names must be unique"
        raise ValueError(msg)
    unknown = overrides.keys() - names
    if unknown:
        msg = f"Undeclared uniform values: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    return {spec.name: spec.validate_value(overrides.get(spec.name, spec.default)) for spec in specs}


def _placeholder(match: re.Match[str]) -> str:
    # 保留换行和原始长度, 以便按词法位置应用替换.
    return re.sub(r"[^\r\n]", " ", match[0])


def _declarations(glsl_code: str, specs: Sequence[ParameterSpec]) -> tuple[re.Match[str], ...]:
    code = _COMMENTS.sub(_placeholder, glsl_code)
    body = _DIRECTIVES.sub(_placeholder, code)
    if _NON_STANDALONE.search(body):
        msg = "Each GLSL uniform requires a standalone declaration without a preceding layout or other qualifier"
        raise ValueError(msg)
    matches = tuple(_DECLARATION.finditer(body))
    declared = {match["name"]: match["type"] for match in matches}
    if len(matches) != len(declared) or len(_UNIFORM.findall(code)) != len(matches) or declared != {spec.name: spec.type for spec in specs}:
        msg = "Each GLSL uniform requires a matching ParameterSpec and a single standalone declaration of the same type"
        raise ValueError(msg)
    return matches


def _literal(type_name: str, value: UniformValue) -> str:
    if isinstance(value, tuple):
        return f"{type_name}({', '.join(repr(part) for part in value)})"
    return repr(value)


def bake_uniforms(glsl_code: str, specs: Sequence[ParameterSpec], uniforms: Mapping[str, UniformInput] | None = None) -> str:
    """用实际参数值生成可独立运行的单文件 ShaderToy GLSL.

    Args:
        glsl_code: 带独立 uniform 声明的参数化源代码.
        specs: 程序验证过的完整参数合同.
        uniforms: 需写入常量的赋值, 未提供的值取合同默认值.

    Returns:
        将外置声明替换为常量的 GLSL.

    Raises:
        ValueError: 合同、赋值或 GLSL 声明无法一一匹配.
    """
    values = resolve_uniforms(specs, uniforms)
    for match in reversed(_declarations(glsl_code, specs)):
        keyword_end, end = match.end("keyword"), match.start("end")
        separator = match.end("name")
        literal = _literal(match["type"], values[match["name"]])
        declaration = "const" + glsl_code[keyword_end:separator] + f" = {literal}" + glsl_code[separator:end]
        glsl_code = glsl_code[: match.start()] + declaration + glsl_code[end:]
    return glsl_code
