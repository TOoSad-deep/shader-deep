"""调参任务和工具共享的只读参考, 不携带文件路径或目标参数."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image

from shader_deep.infrastructure.llm.messages import png_data_url

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ParameterReference:
    """已校验的不可变 PNG; 同一对象可传给任务和工具构建函数.

    Attributes:
        png: 原始 PNG 字节, 不缩放或重编码.
    """

    png: bytes

    def __post_init__(self) -> None:
        """拒绝非 PNG 或无法解码的参考内容."""
        if not isinstance(self.png, bytes):
            msg = "Reference content must be immutable PNG bytes"
            raise TypeError(msg)
        try:
            with Image.open(BytesIO(self.png)) as image:
                if image.format != "PNG":
                    msg = "Reference must be a PNG image"
                    raise ValueError(msg)
                image.load()
        except OSError as error:
            msg = "Unable to decode reference PNG"
            raise ValueError(msg) from error

    @property
    def data_url(self) -> str:
        """返回可直接进入模型请求的原始图片内容."""
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")

    @property
    def sha256(self) -> str:
        """返回冻结内容身份, 与路径及后续文件修改无关."""
        return hashlib.sha256(self.png).hexdigest()


def freeze_parameter_reference(source: Path | ParameterReference) -> ParameterReference:
    """只读取一次参考文件, 或复用已经冻结的对象.

    Args:
        source: 本地 PNG 路径或共享的冻结参考.

    Returns:
        可供任务及 Agent 共同绑定的参考对象.

    Raises:
        ValueError: 路径或图像格式无效.
        OSError: 读取或解码失败.
    """
    if isinstance(source, ParameterReference):
        return source
    return ParameterReference(base64.b64decode(png_data_url(source).split(",", 1)[1]))
