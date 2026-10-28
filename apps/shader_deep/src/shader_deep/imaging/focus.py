"""固定原图目标框的确定性边界校验, 无 Agent 或工具依赖."""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from shader_deep.domain.evidence import ImageRegion


def validate_target_box(reference_url: str, box: ImageRegion | None) -> None:
    """在冻结规划前拒绝越界框, 不生成制品或调用模型."""
    if box is None:
        return
    with Image.open(io.BytesIO(base64.b64decode(reference_url.split(",", 1)[1], validate=True))) as image:
        if box.right > image.width or box.bottom > image.height:
            msg = "目标框超出固定原图边界"
            raise ValueError(msg)
