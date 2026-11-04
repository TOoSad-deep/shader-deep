"""调参模型的 Chat Completions 流完整性检查, 不替换调用方模型的其他行为."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, cast

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from shader_deep.infrastructure.llm.openrouter import StreamTerminal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk


class ParameterOutputError(RuntimeError):
    """模型输出不完整, 不允许将其中的工具参数交给浏览器执行."""


class _StreamCheck:
    """按请求收集结束标记和参数原文, 不依赖 SDK 补齐后的 JSON."""

    def __init__(self) -> None:
        self.terminal = StreamTerminal()
        self.arguments: dict[int | None, str] = {}
        self.failure: ParameterOutputError | None = None

    def accept(self, chunk: ChatGenerationChunk) -> None:
        # 先排空当前响应, 让 SDK 在自身上下文里释放连接. 首个协议错误保留到
        # 完整接收结束后抛出, 图仍不会获得可执行的最终工具调用.
        if self.failure is not None:
            return
        try:
            self._accept(chunk)
        except ParameterOutputError as exc:
            self.failure = exc

    def _accept(self, chunk: ChatGenerationChunk) -> None:
        message = chunk.message
        if self.terminal.metadata and (message.content or (isinstance(message, AIMessageChunk) and message.tool_call_chunks)):
            msg = "Parameter model stream emitted content after its finish marker"
            raise ParameterOutputError(msg)
        if isinstance(message, AIMessageChunk):
            for call in message.tool_call_chunks:
                index = call.get("index")
                self.arguments[index] = self.arguments.get(index, "") + (call.get("args") or "")
        try:
            # 相同结束帧只保留一次; 避免 LangChain 将 length 拼成 lengthlength.
            self.terminal.accept(chunk.generation_info)
        except ValueError as exc:
            msg = "Parameter model stream returned conflicting finish markers"
            raise ParameterOutputError(msg) from exc

    def finish(self) -> None:
        if self.failure is not None:
            raise self.failure
        reason = self.terminal.metadata.get("finish_reason")
        if reason not in {"stop", "tool_calls"}:
            msg = f"Parameter model stream did not finish normally: {reason!r}"
            raise ParameterOutputError(msg)
        if bool(self.arguments) != (reason == "tool_calls"):
            msg = "Parameter model stream finish marker does not match its tool calls"
            raise ParameterOutputError(msg)
        for arguments in self.arguments.values():
            try:
                value = json.loads(arguments)
            except (ValueError, TypeError) as exc:
                msg = "Parameter model stream contains incomplete tool arguments"
                raise ParameterOutputError(msg) from exc
            if not isinstance(value, dict):
                msg = "Parameter model tool arguments must be a JSON object"
                raise ParameterOutputError(msg)


class _ParameterStreamGuard(ChatOpenAI):
    """与原 ChatOpenAI 子类组成 MRO, 保留其载荷和传输覆盖方法."""

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> Iterator[ChatGenerationChunk]:
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            yield from super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            return
        check = _StreamCheck()
        stream = super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
        try:
            for chunk in stream:
                check.accept(chunk)
                if check.failure is None:
                    yield chunk
            check.finish()
        except Exception as error:
            _preserve_stream_failure(check, error)
            raise
        finally:
            # 校验在上游 yield 之后失败时, 显式退出 SDK 的响应上下文.
            primary = sys.exception()
            try:
                if callable(close := getattr(stream, "close", None)):
                    close()
            except Exception as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"Parameter stream cleanup also failed: {type(cleanup).__name__}: {cleanup}")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk
            return
        check = _StreamCheck()
        stream = super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
        try:
            async for chunk in stream:
                check.accept(chunk)
                if check.failure is None:
                    yield chunk
            check.finish()
        except Exception as error:
            _preserve_stream_failure(check, error)
            raise
        finally:
            primary = sys.exception()
            try:
                if callable(close := getattr(stream, "aclose", None)):
                    await close()
            except Exception as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"Parameter stream cleanup also failed: {type(cleanup).__name__}: {cleanup}")


def _preserve_stream_failure(check: _StreamCheck, error: Exception) -> None:
    if check.failure is not None and error is not check.failure:
        check.failure.add_note(f"Parameter stream also failed: {type(error).__name__}: {error}")
        raise check.failure from None


@lru_cache
def _guarded_type(original: type[ChatOpenAI]) -> type[ChatOpenAI]:
    # 只缓存模型类, 不缓存实例/密钥. super() 仍经过原子类, 不丢失用户覆盖方法.
    return cast("type[ChatOpenAI]", type(f"Parameter{original.__name__}", (_ParameterStreamGuard, original), {"__module__": __name__}))


def guard_parameter_stream(model: ChatOpenAI) -> ChatOpenAI:
    """复制模型为带流检查的同类适配器, 不新建或关闭网络连接.

    Args:
        model: 已校验配置的 OpenAI 兼容模型.

    Returns:
        保留原类方法与私有字段的模型副本; 重复配置时不重复包裹类型.
    """
    if isinstance(model, _ParameterStreamGuard):
        return model.model_copy()
    # model_construct 仍会重放 model_post_init/PrivateAttr 工厂, 可能重复创建资源.
    # 浅复制保留已有资源身份, 再附加无实例字段的 guard 类, 不运行初始化钩子.
    guarded = model.model_copy()
    object.__setattr__(guarded, "__class__", _guarded_type(type(model)))
    return guarded


def is_parameter_stream_guarded(model: ChatOpenAI) -> bool:
    """判断模型是否已完成本模块配置, 避免二次包装改变注入客户端的超时."""
    return isinstance(model, _ParameterStreamGuard)
