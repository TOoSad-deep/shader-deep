"""记录网络请求的重试过程, 恢复边界仅包含模型调用, 不重放已执行工具."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

import httpx2
from langchain_core.exceptions import ModelError

from shader_deep.analysis.types import RequestAttempt

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_openai import ChatOpenAI

    from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions

Response = TypeVar("Response")
MAX_CAUSE_DEPTH = 5


def configure_analysis_model(model: ChatOpenAI, options: AnalysisOptions) -> ChatOpenAI:
    """关闭 SDK 内部重试, 保留已配置的模型服务与追踪链路.

    Args:
        model: 使用用户现有完整配置组创建的模型客户端.
        options: 分析流程专用的请求超时与输出预算.

    Returns:
        共享现有传输连接、请求尝试次数受限的同步模型客户端.
    """
    # with_options 共享 LangChain 缓存的 HTTP 连接; 此处不能关闭连接,
    # 因为其他并发子任务可能仍在使用该连接发送请求.
    client = model.root_client.with_options(max_retries=0, timeout=float(options.request_timeout_seconds))
    legacy_tokens = model.model_name.lower().startswith("deepseek")
    extra_body = dict(model.extra_body or {})
    if legacy_tokens:
        # 已有联调注释记录: 当前 DeepSeek 接口忽略 max_completion_tokens,
        # 曾以 128 token 对照请求确认 max_tokens 生效, 因此在 extra_body 中设置它.
        extra_body["max_tokens"] = options.max_output_tokens
    return model.model_copy(
        update={
            "root_client": client,
            "client": client.chat.completions,
            "max_retries": 0,
            "request_timeout": float(options.request_timeout_seconds),
            "streaming": options.stream_model_responses,
            "max_tokens": None if legacy_tokens else options.max_output_tokens,
            "extra_body": extra_body or None,
        }
    )


def call_with_recovery(call: Callable[[], Response], execution: AnalysisExecution, retries: int) -> Response:
    """只重试暂时性模型请求错误, 并保留任务身份与每次尝试记录.

    Args:
        call: 单次模型调用; 重试边界内不能包含工具执行.
        execution: 当前角色的调用计数与审计记录.
        retries: 本次逻辑调用允许的额外请求尝试次数.

    Returns:
        首次成功返回的响应.

    Raises:
        Exception: 不可重试的服务错误, 或请求尝试次数耗尽时的最后一次错误.
    """
    for attempt in range(retries + 1):
        # monotonic 不受系统时钟校准影响; 成功和失败都记录耗时, 便于区分逻辑轮与请求次数.
        began = time.monotonic()
        try:
            result = call()
        except Exception as exc:  # 记录所有失败, 仅对判定为暂时性的模型请求错误重试.
            execution.request_attempts += (_attempt(execution.model_calls, attempt, began, exc),)
            if not _retryable(exc) or attempt == retries:
                raise
            # 退避为 1、2、4、4…秒, 减少暂时性服务错误下的连续请求压力.
            time.sleep(min(2.0**attempt, 4.0))
        else:
            execution.request_attempts += (_attempt(execution.model_calls, attempt, began, None),)
            return result
    msg = "No request attempts were permitted"
    raise RuntimeError(msg)


def _retryable(error: Exception) -> bool:
    """区分暂时性传输故障与重试无法解决的本地配置、客户端错误."""
    # 收到 SSE 响应头后, SDK 可能直接抛出读取连接时的传输异常.
    # 先完整接收并校验模型响应, 避免在半截工具参数上执行工具.
    if isinstance(error, (httpx2.NetworkError, httpx2.TimeoutException, httpx2.RemoteProtocolError)):
        return True
    if not isinstance(error, ModelError) or not error.is_retryable:
        return False
    # SDK 也可能将本地客户端问题包装成 APIConnectionError.
    # 客户端已关闭或配置错误无法靠重试恢复, 因此需检查异常原因链.
    cause = error.__cause__
    for _ in range(MAX_CAUSE_DEPTH):
        if cause is None:
            return True
        if isinstance(cause, (RuntimeError, TypeError, ValueError)):
            return False
        cause = cause.__cause__
    return True


def _attempt(model_call: int, attempt: int, began: float, error: Exception | None) -> RequestAttempt:
    """提取异常类型链与耗时; 对外记录从 1 开始的尝试序号."""
    causes = []
    cause = error.__cause__ if error is not None else None
    while cause is not None and len(causes) < MAX_CAUSE_DEPTH:
        causes.append(type(cause).__name__)
        cause = cause.__cause__
    return RequestAttempt(
        model_call=model_call,
        attempt=attempt + 1,
        elapsed_seconds=round(time.monotonic() - began, 3),
        error_type=type(error).__name__ if error else None,
        cause_types=tuple(causes),
    )
