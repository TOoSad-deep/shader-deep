"""适配模型请求与完整响应校验, 为 SDK 请求重试识别暂时性网络错误."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx2
from langchain_core.exceptions import ModelAPIError, ModelError
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.tool import tool_call
from langchain_openai import ChatOpenAI

# 本模块只适配请求/响应协议. 原生角色的 ModelRetryMiddleware 决定是否重试请求,
# 业务工具是否接受结果仍由角色和领域层决定, 不在传输层重放任务.
from shader_deep.infrastructure.llm.openrouter import (
    RAW_REASONING,
    ReasoningBuffer,
    StreamTerminal,
    _tool_parameters,
    is_openrouter,
    restore_reasoning,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk, ChatResult
    from openai import BaseModel

    from shader_deep.infrastructure.llm.options import ModelOptions

MAX_CAUSE_DEPTH = 5
RAW_TOOL_CALLS = "analysis_raw_tool_calls"


def _reject_constant(value: str) -> object:
    """拒绝 JSON 标准未定义的 NaN 和 Infinity."""
    msg = f"Nonstandard JSON constant: {value}"
    raise ValueError(msg)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """重复键具有覆盖歧义, 不接受解析器静默选择最后一个值."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"Duplicate JSON key: {key[:80]}"
            raise ValueError(msg)
        result[key] = value
    return result


def decode_object(raw: str) -> dict[str, object]:
    """严格解码一个完整工具参数对象, 不使用底层流式解析器补齐的结果.

    Args:
        raw: 模型服务返回的完整原始参数字符串.

    Returns:
        未补全或删改字段的 JSON 对象.

    Raises:
        ValueError: 参数不是严格、完整且无重复键的 JSON.
        TypeError: 参数的顶层不是 JSON 对象.
    """
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        msg = "Tool arguments must be one JSON object"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


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
        """保留本地契约, 在请求边界适配 required 和 OpenRouter 非空白正则."""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "tools" in payload:
            payload["tools"] = _explicit_required(payload["tools"])
        if is_openrouter(self.openai_api_base):
            for entry in payload.get("tools", []):
                if entry.get("type") == "function":
                    # 保留动态 F/R 选择键的原 Schema, 不将字典的 additionalProperties 改为 false.
                    entry["function"]["strict"] = True
                    if "parameters" in entry["function"]:
                        entry["function"]["parameters"] = _tool_parameters(entry["function"]["parameters"])
            restore_reasoning(payload, self._convert_input(input_).to_messages())
        return payload

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        """先保留网关推理增量, 完整汇集后才交给框架消息历史."""
        converted = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices", [])
        if converted is not None and choices and is_openrouter(self.openai_api_base):
            details = (choices[0].get("delta") or {}).get("reasoning_details")
            if isinstance(details, list):
                converted.message.additional_kwargs[RAW_REASONING] = details
        return converted

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> Iterator[ChatGenerationChunk]:
        """随流式片段保存参数原文, 由框架按 index 合并而不重新序列化参数."""
        reasoning = ReasoningBuffer()
        terminal = StreamTerminal()
        # 每次网络请求独立汇集; 部分 chunk 可进入框架, 但完整响应校验前不能执行工具.
        for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            details = chunk.message.additional_kwargs.pop(RAW_REASONING, [])
            reasoning.append(details)
            finished = terminal.accept(chunk.generation_info) if is_openrouter(self.openai_api_base) else False
            if finished and reasoning.blocks:
                chunk.message.additional_kwargs["reasoning_details"] = reasoning.details()
            if isinstance(chunk.message, AIMessageChunk) and chunk.message.tool_call_chunks:
                # 额外保存参数原文, 因为 SDK 的增量 JSON 解析可能提前补齐末尾括号.
                chunk.message.additional_kwargs[RAW_TOOL_CALLS] = [dict(call) for call in chunk.message.tool_call_chunks]
            yield chunk

    def _create_chat_result(self, response: dict | BaseModel, generation_info: dict | None = None) -> ChatResult:
        """非流式响应同样保留原始参数; 不依赖适配器已解析的工具调用."""
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data.get("choices", []), strict=True):
            if isinstance(generation.message, AIMessage):
                details = choice.get("message", {}).get("reasoning_details")
                if is_openrouter(self.openai_api_base) and isinstance(details, list):
                    generation.message.additional_kwargs["reasoning_details"] = details
                generation.message.additional_kwargs[RAW_TOOL_CALLS] = [
                    {"id": call.get("id"), "name": call.get("function", {}).get("name"), "args": call.get("function", {}).get("arguments")}
                    for call in choice.get("message", {}).get("tool_calls", []) or []
                ]
        return result


def validate_analysis_message(message: AIMessage) -> AIMessage:
    """工具执行前核对结束标记与原始 JSON, 不使用流式解析器猜补的参数.

    Args:
        message: 完整接收后的模型响应, 包含适配器保存的原始参数.

    Returns:
        仅含严格原始 JSON 解码结果的消息.

    Raises:
        ModelAPIError: 工具响应缺少正常结束标记, 可由框架有限重试.
        ValueError: 输出截断、身份或参数不是完整且无歧义的 JSON 对象.
        TypeError: 原始工具参数缺失或类型不正确.
    """
    finish = message.response_metadata.get("finish_reason")
    if finish == "length":
        msg = "Analysis response reached output limit; no tools executed"
        raise ValueError(msg)
    raw = message.additional_kwargs.get(RAW_TOOL_CALLS, [])
    if not (raw or message.tool_calls or message.invalid_tool_calls):
        return message
    if finish not in {"stop", "tool_calls"}:
        # 缺少正常结束标记按传输不完整处理, 让框架在限定次数内重新请求.
        msg = "Incomplete model response: no successful finish marker for tool arguments"
        raise ModelAPIError(msg)
    return _strict_tool_calls(message, raw)


def _strict_tool_calls(message: AIMessage, raw: object) -> AIMessage:
    """整条响应通过严格检查后才允许执行其中任何工具."""
    if not isinstance(raw, list) or not raw:
        msg = "Missing original tool arguments; no tools executed"
        raise ValueError(msg)
    accepted = []
    identities: set[str] = set()
    # 先严格解析整条响应的所有调用, 再统一替换 SDK 解析结果, 不执行半批合法参数.
    for call in raw:
        if not isinstance(call, dict) or not isinstance(call.get("args"), str):
            msg = "Missing original JSON tool arguments; no tools executed"
            raise TypeError(msg)
        identity, name = call.get("id"), call.get("name")
        if not isinstance(identity, str) or not identity or identity in identities or not isinstance(name, str) or not name:
            msg = "Missing or ambiguous tool call identity; no tools executed"
            raise ValueError(msg)
        arguments = decode_object(call["args"])
        identities.add(identity)
        accepted.append(tool_call(name=name, args=arguments, id=identity))
    extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
    # 消除旧序列化调用副本, 保留推理等协议字段; 后续发回历史时以严格解析后的调用为准.
    return message.model_copy(update={"tool_calls": accepted, "invalid_tool_calls": [], "additional_kwargs": extra})


def configure_analysis_model(model: ChatOpenAI, options: ModelOptions) -> ChatOpenAI:
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
    legacy_tokens = model.model_name.lower().startswith("deepseek") or is_openrouter(model.openai_api_base)
    extra_body = dict(model.extra_body or {})
    if legacy_tokens:
        # OpenRouter 使用网关 max_tokens; 当前 DeepSeek 接口忽略 max_completion_tokens,
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
