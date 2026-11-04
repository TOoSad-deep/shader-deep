"""调参实验的模型请求保护, 重试范围不包含已经执行的工具."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware, ModelRetryMiddleware
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai.resources.chat.completions import AsyncCompletions, Completions

from shader_deep.experiments._parameter_stream import (
    ParameterOutputError as ParameterOutputError,
    guard_parameter_stream,
    is_parameter_stream_guarded,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain_core.language_models import BaseChatModel


def _client_options(model: ChatOpenAI) -> dict[str, object]:
    # 标准资源持有真实 SDK owner. 从 owner 派生副本, 保留注入的鉴权、地址、
    # headers 和 HTTP transport. 不透明自定义资源保持原样, 不猜测其配置协议.
    options: dict[str, object] = {}
    if type(model.client) is Completions:
        owner = model.client._client
        timeout = model.request_timeout if model.request_timeout is not None else owner.timeout if model.root_client is None else 45.0
        client = owner.with_options(max_retries=0, timeout=timeout)
        options.update(root_client=client, client=client.chat.completions)
    if type(model.async_client) is AsyncCompletions:
        owner = model.async_client._client
        timeout = model.request_timeout if model.request_timeout is not None else owner.timeout if model.root_async_client is None else 45.0
        client = owner.with_options(max_retries=0, timeout=timeout)
        options.update(root_async_client=client, async_client=client.chat.completions)
    return options


def configure_parameter_model(model: BaseChatModel) -> BaseChatModel:
    """复制 OpenAI 兼容客户端的实验设置, 保留显式输出预算和用户传输配置.

    Args:
        model: 已有模型, 不原地修改或关闭调用方持有的 HTTP 连接.

    Returns:
        缺省输出上限 8192 的模型副本. 标准 SDK 资源关闭内部重试, 常规模型
        缺省超时为 45 秒; 直接注入的 SDK 资源保留其已有超时.
        自定义 completions 资源保留原样, 其内部重试/超时由调用方管理.
        其他类型的模型原样返回.
    """
    if not isinstance(model, ChatOpenAI):
        return model
    if is_parameter_stream_guarded(model):
        return model.model_copy()
    options = _client_options(model)
    options["max_tokens"] = model.max_tokens if model.max_tokens is not None else 8192
    standard = (model.client is None or "client" in options) and (model.async_client is None or "async_client" in options)
    if standard:
        options["max_retries"] = 0
    if standard and model.request_timeout is None and model.root_client is not None and model.root_async_client is not None:
        options["request_timeout"] = 45.0
    return guard_parameter_stream(model).model_copy(update=options)


def _connection_failure(error: Exception) -> bool:
    # 项目现有客户端可使用 httpx 或 httpx2; 沿异常链识别建连错误,
    # 不把本地配置、认证错误或已发出请求后的读取超时当成建连失败.
    cause: BaseException | None = error
    for _ in range(8):
        if cause is None:
            break
        kind = type(cause)
        if kind.__module__.split(".")[0] in {"httpx", "httpx2", "httpcore"} and kind.__name__ in {"ConnectTimeout", "ConnectError"}:
            return True
        cause = cause.__cause__
    return False


def connection_retry() -> ModelRetryMiddleware:
    """返回原生请求级重试: 建连失败等待一秒, 最多额外尝试一次.

    Returns:
        失败耗尽后抛出原异常的 middleware, 不返回伪成功 AIMessage.
    """
    return ModelRetryMiddleware(max_retries=1, retry_on=_connection_failure, on_failure="error", initial_delay=1.0, jitter=False)


def _complete(response: ModelResponse) -> ModelResponse:
    for message in response.result:
        if isinstance(message, AIMessage) and message.response_metadata.get("finish_reason") == "length":
            msg = "Parameter model output was truncated; no tools from this response were executed"
            raise ParameterOutputError(msg)
    return response


class CompleteParameterResponse(AgentMiddleware):
    """在工具调度之前检查完整回复, 同步和异步入口使用相同规则."""

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """完整接收单次回复后拒绝截断, 原始用量仍保留在模型回调/追踪中."""
        return _complete(handler(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步调用同样在返回工具参数前检查截断标记."""
        return _complete(await handler(request))
