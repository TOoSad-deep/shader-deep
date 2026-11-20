"""评审图的工具准入、有序批次与有限执行保护."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import TYPE_CHECKING, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from shader_deep.agents.review.rejection import rejection_diagnostic
from shader_deep.domain.visual_review import ReviewValidationError
from shader_deep.infrastructure.storage.snapshots import write_snapshot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import BaseMessage
    from langgraph.runtime import Runtime
    from langgraph.types import Command

    from shader_deep.workflows.review.session import ReviewSession


@dataclass
class _ToolBatch:
    """串行执行一条模型消息中的有效工具, 异常时唤醒后续等待者."""

    key: str
    ids: tuple[str, ...]
    failed: bool = False
    sync: list[Event] = field(init=False)
    asynchronous: list[asyncio.Event] = field(init=False)

    def __post_init__(self) -> None:
        self.sync = [Event() for _ in self.ids]
        self.asynchronous = [asyncio.Event() for _ in self.ids]


class ReviewGuard(AgentMiddleware[AgentState[object], None]):
    """工具执行侧再次检查白名单, 接受后在下一次主请求之前结束."""

    def __init__(self, session: ReviewSession, allowed_tools: frozenset[str]) -> None:
        """绑定本次短会话."""
        self.session = session
        self.allowed_tools = allowed_tools
        self._lock = Lock()
        self._batch: _ToolBatch | None = None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """终态与预算检查先于模型及后续包装中间件."""
        del state, runtime
        return None if self.session.begin_model_call() else {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """异步调用采用相同的逻辑预算与终态."""
        return self.before_model(state, runtime)

    def _error(self, request: ToolCallRequest, content: dict[str, str]) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(content, ensure_ascii=False), tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error"
        )

    def _refusal(self, request: ToolCallRequest, *, aborted: bool = False) -> ToolMessage | None:
        if self.session.finished:
            reason = "评审会话已结束"
        elif request.tool_call["name"] not in self.allowed_tools:
            reason = f"本轮只允许 {', '.join(sorted(self.allowed_tools))}"
        elif aborted:
            reason = "前序工具执行异常, 本批后续工具已停止"
        else:
            return None
        self.session.record_rejection(validation=False)
        return self._error(request, {"error": reason})

    def _ordered_batch(self, request: ToolCallRequest) -> tuple[_ToolBatch, int]:
        state = cast("Mapping[str, object]", request.state)
        messages = cast("list[BaseMessage]", state.get("messages", []))
        latest = next((entry for entry in reversed(messages) if isinstance(entry, AIMessage)), None)
        calls = latest.tool_calls if latest is not None else [request.tool_call]
        ids = tuple(str(call["id"]) for call in calls if call["name"] in self.allowed_tools)
        key = str(latest.id if latest is not None and latest.id else id(latest))
        with self._lock:
            if self._batch is None or self._batch.key != key:
                self._batch = _ToolBatch(key, ids)
            return self._batch, ids.index(str(request.tool_call["id"]))

    def _validation_error(self, request: ToolCallRequest, error: ValueError) -> ToolMessage:
        self._record_rejection(request, error)
        values = (
            {"code": error.code, "path": error.path, "message": error.message}
            if isinstance(error, ReviewValidationError)
            else {"message": str(error)}
        )
        return self._error(request, values)

    def _record_rejection(self, request: ToolCallRequest, error: ValueError | None) -> None:
        self.session.record_rejection(validation=True)
        if self.session.source.request.protocol == "visual_review_v2":
            with self._lock:
                snapshot = rejection_diagnostic(self.session, request, error)
                try:
                    write_snapshot(self.session.directory / "results" / "last_rejection.json", snapshot)
                except OSError:
                    logging.getLogger(__name__).warning("评审校验诊断未能写入, 继续返回原有工具反馈")

    def _record(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        if isinstance(result, ToolMessage) and result.status == "error":
            self._record_rejection(request, None)
        return result

    def _execute(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        try:
            return self._record(request, handler(request))
        except ValueError as error:
            return self._validation_error(request, error)

    async def _aexecute(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        try:
            return self._record(request, await handler(request))
        except ValueError as error:
            return self._validation_error(request, error)

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """模型声明顺序决定首个有效终态, 后续工具明确拒绝."""
        if request.tool_call["name"] not in self.allowed_tools:
            return self._execute(request, handler)
        batch, index = self._ordered_batch(request)
        if index:
            batch.sync[index - 1].wait()
        try:
            refusal = self._refusal(request, aborted=batch.failed)
            return refusal if refusal is not None else self._execute(request, handler)
        except BaseException:
            batch.failed = True
            raise
        finally:
            batch.sync[index].set()

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """异步工具使用事件等待, 保持顺序而不阻塞事件循环."""
        if request.tool_call["name"] not in self.allowed_tools:
            return await self._aexecute(request, handler)
        batch, index = self._ordered_batch(request)
        if index:
            await batch.asynchronous[index - 1].wait()
        try:
            refusal = self._refusal(request, aborted=batch.failed)
            return refusal if refusal is not None else await self._aexecute(request, handler)
        except BaseException:
            batch.failed = True
            raise
        finally:
            batch.asynchronous[index].set()
