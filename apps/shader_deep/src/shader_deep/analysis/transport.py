"""记录网络请求的重试过程, 恢复边界仅包含模型调用, 不重放已执行工具."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

import httpx2
from langchain_core.exceptions import ModelError
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

from shader_deep.analysis.types import RequestAttempt

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk, ChatResult
    from openai import BaseModel

    from shader_deep.analysis.events import EventCallback
    from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions

Response = TypeVar("Response")
MAX_CAUSE_DEPTH = 5
RAW_TOOL_CALLS = "analysis_raw_tool_calls"


def _explicit_required(schema: object) -> object:
    """显式发送空 required 数组, 避免兼容服务将缺省值解释为 null."""
    if isinstance(schema, list):
        return [_explicit_required(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: _explicit_required(value) for key, value in schema.items()}
    if result.get("type") == "object" and result.get("required") is None:
        result["required"] = []
    return result


class AnalysisChatOpenAI(ChatOpenAI):
    """保留原始参数供执行前审查, 防止流式解析器补齐非法 JSON 后丢失原文."""

    def _get_request_payload(self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: object) -> dict:
        """保持参数可选性, 只补全模型工具 Schema 的合法空 required."""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "tools" in payload:
            payload["tools"] = _explicit_required(payload["tools"])
        return payload

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> Iterator[ChatGenerationChunk]:
        """随流式片段保存参数原文, 由框架按 index 合并而不重新序列化参数."""
        for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            if isinstance(chunk.message, AIMessageChunk) and chunk.message.tool_call_chunks:
                chunk.message.additional_kwargs[RAW_TOOL_CALLS] = [dict(call) for call in chunk.message.tool_call_chunks]
            yield chunk

    def _create_chat_result(self, response: dict | BaseModel, generation_info: dict | None = None) -> ChatResult:
        """非流式响应同样保留原始参数; 不依赖适配器已解析的工具调用."""
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data.get("choices", []), strict=True):
            if isinstance(generation.message, AIMessage):
                generation.message.additional_kwargs[RAW_TOOL_CALLS] = [
                    {"id": call.get("id"), "name": call.get("function", {}).get("name"), "args": call.get("function", {}).get("arguments")}
                    for call in choice.get("message", {}).get("tool_calls", []) or []
                ]
        return result


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
    # 从已校验实例复制字段与共享客户端, 不重新构造网络资源或改动用户配置.
    guarded = AnalysisChatOpenAI.model_construct(**model.__dict__)
    guarded.__pydantic_private__ = model.__pydantic_private__
    return guarded.model_copy(
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


def call_with_recovery(
    call: Callable[[], Response],
    execution: AnalysisExecution,
    retries: int,
    *,
    on_event: EventCallback | None = None,
    before_attempt: Callable[[], None] | None = None,
) -> Response:
    """只重试暂时性模型请求错误, 并保留任务身份与每次尝试记录.

    Args:
        call: 单次模型调用; 重试边界内不能包含工具执行.
        execution: 当前角色的调用计数与审计记录.
        retries: 本次逻辑调用允许的额外请求尝试次数.
        on_event: 可选的短事件回调, 用于观察请求和重试进度.
        before_attempt: 每次网络尝试前检查取消, 被取消的尝试不计作已发送请求.

    Returns:
        首次成功返回的响应.

    Raises:
        Exception: 不可重试的服务错误, 或请求尝试次数耗尽时的最后一次错误.
    """
    for attempt in range(retries + 1):
        if before_attempt is not None:
            before_attempt()
        # monotonic 不受系统时钟校准影响; 成功和失败都记录耗时, 便于区分逻辑轮与请求次数.
        began = time.monotonic()
        if on_event is not None:
            on_event("request_started", {"attempt": attempt + 1})
        try:
            result = call()
        except Exception as exc:  # 记录所有失败, 仅对判定为暂时性的模型请求错误重试.
            record = _attempt(execution.model_calls, attempt, began, exc)
            execution.request_attempts += (record,)
            if on_event is not None:
                on_event("request_failed", {"attempt": attempt + 1, "error_type": record.error_type, "elapsed_seconds": record.elapsed_seconds})
            if not _retryable(exc) or attempt == retries:
                raise
            if on_event is not None:
                on_event("request_retry", {"next_attempt": attempt + 2})
            # 退避为 1、2、4、4…秒, 减少暂时性服务错误下的连续请求压力.
            time.sleep(min(2.0**attempt, 4.0))
        else:
            record = _attempt(execution.model_calls, attempt, began, None)
            execution.request_attempts += (record,)
            if on_event is not None:
                on_event("request_completed", {"attempt": attempt + 1, "elapsed_seconds": record.elapsed_seconds})
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
