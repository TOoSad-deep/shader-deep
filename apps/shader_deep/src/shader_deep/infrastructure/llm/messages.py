"""上下文构造共用的制品读取工具."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    # Path 只用于类型注解; 配合延迟注解, 运行时不需要导入它.
    from pathlib import Path


def png_data_url(path: Path) -> str:
    """读取本地 PNG, 并转换为 Base64 data URL.

    Args:
        path: 本地 PNG 路径.

    Returns:
        保留原始图片字节的 data URL.

    Raises:
        ValueError: 文件不存在或扩展名不是 PNG.
    """
    # 这里只检查文件存在和扩展名, 不解码图片检查像素, 也不缩放或裁剪.
    if not path.is_file():
        msg = f"PNG file does not exist: {path}"
        raise ValueError(msg)
    if path.suffix.lower() != ".png":
        msg = f"Expected a .png file: {path}"
        raise ValueError(msg)
    # bytes -> Base64 bytes -> ASCII 字符串, 才能装进 JSON 请求的 image_url 字段.
    # data URL 自带图片内容, 因此模型服务不需要访问用户机器上的这个文件路径.
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _message(payload: dict[str, object], reference_url: str, control: dict[str, object] | None = None) -> HumanMessage:
    """业务正文和真实原图保持固定, 控制信息不扩充业务协议."""
    content: list[str | dict[str, object]] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        {"type": "image_url", "image_url": {"url": reference_url}},
    ]
    if control:
        content.append({"type": "text", "text": "运行控制反馈: " + json.dumps(control, ensure_ascii=False, separators=(",", ":"))})
    return HumanMessage(content=content)
