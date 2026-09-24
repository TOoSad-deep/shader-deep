"""从环境变量读取模型配置并创建客户端."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


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
