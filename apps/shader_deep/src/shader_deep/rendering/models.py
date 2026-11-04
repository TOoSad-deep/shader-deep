"""渲染模块的公共数据契约与统一异常, 不启动浏览器或线程."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping


class RenderError(RuntimeError):
    """渲染失败, 保留失败阶段与浏览器原始日志.

    Attributes:
        stage: 初始化、编译、链接、绘制或导出等失败阶段.
        log: 底层错误详情, Shader 编译错误保留驱动返回的日志.
    """

    def __init__(self, log: str, *, stage: str) -> None:
        """创建可供调用方诊断的渲染错误.

        Args:
            log: 该阶段的错误详情.
            stage: 失败阶段.
        """
        self.stage = stage
        self.log = log
        super().__init__(f"{stage}: {log}")


class ParameterSpec(TypedDict):
    """允许调整的 uniform float 配置.

    Attributes:
        min: 允许的最小值, 包含端点.
        max: 允许的最大值, 包含端点.
        initial: 初值, 必须位于允许范围内.
    """

    min: float
    max: float
    initial: float


@dataclass(frozen=True)
class PreviewSnapshot:
    """一次取图产生的一致快照, 可脱离浏览器保存或传给模型.

    Attributes:
        png: 直接由画布导出的 PNG 字节.
        parameters: 该帧使用的完整参数, 只读且不随后续调参变化.
        revision: 参数版本, 每次成功更新递增.
        time: 该帧实际使用的 iTime 秒数.
        width: PNG 像素宽度.
        height: PNG 像素高度.
    """

    png: bytes
    parameters: Mapping[str, float]
    revision: int
    time: float
    width: int
    height: int

    def __post_init__(self) -> None:
        """在契约自身保证参数快照独立且只读, 不依赖调用方正确复制."""
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class _PreviewConfig:
    """经过校验并复制的会话配置, 仅在渲染模块内部传递."""

    glsl_code: str
    parameters: dict[str, ParameterSpec]
    width: int
    height: int
    time: float | None
    headless: bool
