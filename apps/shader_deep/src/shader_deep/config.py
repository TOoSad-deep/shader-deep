"""从环境变量读取模型配置并创建客户端."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_openai import ChatOpenAI

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class GenerationOptions:
    """单次生成运行的渲染条件和预算.

    Attributes:
        width: 每个候选的固定渲染宽度.
        height: 每个候选的固定渲染高度.
        time: 固定传给 iTime 的秒数.
        max_attempts: 最多提交多少个渲染候选, 失败也计数.
        output_dir: 运行目录的父目录, 未提供时使用当前目录下的 runs.
    """

    width: int = 512
    height: int = 512
    time: float = 0.0
    max_attempts: int = 3
    output_dir: Path | None = None

    def __post_init__(self) -> None:
        """在启动模型或浏览器前检查预算与渲染条件."""
        # dataclass 初始化后自动调用; 尽早拒绝无效配置, 避免发起无意义的模型请求.
        # bool 是 int 的子类, 因此必须单独排除 True/False 作为尺寸或次数.
        for name in ("width", "height", "max_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                msg = f"{name} must be a positive integer"
                raise ValueError(msg)
        if isinstance(self.time, bool) or not isinstance(self.time, (int, float)) or not math.isfinite(self.time):
            msg = "time must be a finite number of seconds"
            raise ValueError(msg)


def require_env(name: str) -> str:
    """读取一个必填且不能为空的环境变量.

    Args:
        name: 要读取的环境变量名称.

    Returns:
        去除首尾空白的配置值.

    Raises:
        ValueError: 环境变量未设置或内容为空白.
    """
    value = os.getenv(name, "").strip()
    if not value:
        msg = f"Missing required environment variable: {name}"
        raise ValueError(msg)
    return value


def build_model() -> ChatOpenAI:
    """优先使用 DeepSeek 专用配置, 否则使用既有 MICU 配置创建聊天模型.

    Returns:
        使用现有 Chat Completions 配置的聊天客户端.

    Raises:
        ValueError: 所选配置组的模型、地址或密钥缺失, 不会从另一组补齐.
    """
    # 这里只创建客户端; 真正的网络请求由 Agent 调用模型时发起.
    # 应用读取进程环境变量, .env 的加载由启动命令承担.
    # 一旦配置 DS_MICU 组就整组使用, 防止 DeepSeek 模型与另一组 API key 混用.
    prefix = "DS_MICU" if any(os.getenv(f"DS_MICU_{field}", "").strip() for field in ("MODEL", "BASE_URL", "API_KEY")) else "MICU"
    # 显式沿用 Chat Completions 协议, 与当前中转接口及工具调用测试保持一致.
    return ChatOpenAI(
        model=require_env(f"{prefix}_MODEL"),
        base_url=require_env(f"{prefix}_BASE_URL"),
        api_key=require_env(f"{prefix}_API_KEY"),
        use_responses_api=False,
    )
