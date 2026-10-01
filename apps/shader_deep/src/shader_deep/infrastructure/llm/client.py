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


def _configuration_prefix() -> str:
    """按优先级整组选用配置, 不跨组补齐凭据."""
    for prefix in ("OPENROUTER", "DS_MICU"):
        if any(os.getenv(f"{prefix}_{field}", "").strip() for field in ("MODEL", "BASE_URL", "API_KEY")):
            return prefix
    return "MICU"


def _openrouter_body() -> dict[str, object]:
    """传递显式推理及选路设置, 未配置时沿用提供方默认值."""
    provider: dict[str, object] = {"require_parameters": True}
    sorting = os.getenv("OPENROUTER_PROVIDER_SORT", "").strip()
    if sorting:
        if sorting not in {"price", "throughput", "latency"}:
            msg = "Invalid OPENROUTER_PROVIDER_SORT"
            raise ValueError(msg)
        provider["sort"] = sorting
    body: dict[str, object] = {"provider": provider}
    effort = os.getenv("OPENROUTER_REASONING_EFFORT", "").strip()
    if effort:
        if effort not in {"max", "xhigh", "high", "medium", "low", "minimal", "none"}:
            msg = "Invalid OPENROUTER_REASONING_EFFORT"
            raise ValueError(msg)
        body["reasoning"] = {"effort": effort}
    return body


def build_model() -> ChatOpenAI:
    """依次选择 OpenRouter、DeepSeek 专用或 MICU 整组聊天配置.

    Returns:
        使用现有 Chat Completions 配置的聊天客户端.

    Raises:
        ValueError: 所选配置组的模型、地址或密钥缺失, 不会从另一组补齐.
    """
    # 这里只创建客户端; 真正的网络请求由 Agent 调用模型时发起.
    # 应用读取进程环境变量, .env 的加载由启动命令承担.
    prefix = _configuration_prefix()
    # 显式沿用 Chat Completions 协议, 与当前中转接口及工具调用测试保持一致.
    return ChatOpenAI(
        model=require_env(f"{prefix}_MODEL"),
        base_url=require_env(f"{prefix}_BASE_URL"),
        api_key=require_env(f"{prefix}_API_KEY"),
        use_responses_api=False,
        # OpenRouter 只路由到支持实际请求参数的提供方, 防止静默忽略工具配置.
        extra_body=_openrouter_body() if prefix == "OPENROUTER" else None,
    )
