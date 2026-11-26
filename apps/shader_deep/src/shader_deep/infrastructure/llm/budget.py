"""在模型请求发出前预留跨阶段额度, 收到正式用量后结算."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from threading import Condition, get_ident
from typing import TYPE_CHECKING, TypeGuard

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable  # noqa: TC002  # Pydantic 需解析注入模型的 Runnable 字段.
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from shader_deep.infrastructure.llm.openrouter import OpenRouterChatOpenAI, is_openrouter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_core.runnables import RunnableConfig
    from openai import BaseModel

DEFAULT_OUTPUT_BUDGET = 256_000
_DEFAULT_REQUEST_OUTPUT = 16_384


class ModelBudgetExhaustedError(RuntimeError):
    """没有足够的全局额度发送下一次请求."""


class _BudgetBusyError(ModelBudgetExhaustedError):
    def __init__(self, meter: RequestBudget) -> None:
        self.meter = meter
        super().__init__("Global token budget is temporarily reserved by active requests")


@dataclass(frozen=True)
class TokenBalance:
    """剩余可分配额度与服务端确认的耗用, 两者分开记录."""

    output: int
    total: int | None
    input_used: int
    output_used: int
    attempts: int
    unconfirmed: int


@dataclass(frozen=True, eq=False)
class _Reservation:
    meter: RequestBudget
    output: int
    total: int | None
    thread: int
    task: object


def _token_counts(prompt: object, completion: object, total: object) -> tuple[int, int, int] | None:
    if type(prompt) is not int or type(completion) is not int or type(total) is not int:
        return None
    if min(prompt, completion) < 0 or total < prompt + completion:
        return None
    return prompt, completion, total


class RequestBudget:
    """同一工作流的并发请求共享一把锁, 预留也随快照持久化."""

    def __init__(self, balance: TokenBalance, context_limit: int | None, save: Callable[[TokenBalance], None]) -> None:
        """加载上次保存的余额及部署申明的总量上界."""
        self.balance = balance
        self.context_limit = context_limit
        self.save = save
        self._condition = Condition()
        self._in_flight: set[_Reservation] = set()
        self._async_waiters: dict[asyncio.Event, asyncio.AbstractEventLoop] = {}
        self.exhausted: str | None = None

    def _persist(self, balance: TokenBalance) -> None:
        self.save(balance)
        self.balance = balance

    def _can_fund_request(self) -> bool:
        balance = self.balance
        return balance.output > 0 and (balance.total is None or (self.context_limit is not None and balance.total >= self.context_limit))

    def _temporarily_reserved(self) -> bool:
        return self.exhausted is None and not self._can_fund_request() and bool(self._in_flight)

    async def _await_capacity(self) -> None:
        with self._condition:
            if not self._temporarily_reserved():
                return
            ready = asyncio.Event()
            self._async_waiters[ready] = asyncio.get_running_loop()
        try:
            await ready.wait()
        finally:
            with self._condition:
                self._async_waiters.pop(ready, None)

    def reserve(self, requested: int, *, asynchronous: bool = False) -> _Reservation:
        """预留一次物理尝试。启用总量时按部署的完整上下文上界预留."""
        thread = get_ident()
        task = asyncio.current_task() if asynchronous else None
        with self._condition:
            while self._temporarily_reserved():
                self._wait_for_other_requests(thread, task, asynchronous=asynchronous)
            if self.exhausted or not self._can_fund_request():
                self.exhausted = self.exhausted or "Global token budget cannot fund another model request"
                raise ModelBudgetExhaustedError(self.exhausted)
            return self._reserve(requested, thread, task)

    def _wait_for_other_requests(self, thread: int, task: object, *, asynchronous: bool) -> None:
        # 同一调用者可暂停一条流再发下一次请求, 此时不能等它自己继续结算.
        if any(ticket.thread == thread and (not asynchronous or ticket.task is task) for ticket in self._in_flight):
            msg = "Global token budget is temporarily reserved by an active request in this execution"
            raise ModelBudgetExhaustedError(msg)
        if asynchronous:
            raise _BudgetBusyError(self)
        self._condition.wait()

    def _reserve(self, requested: int, thread: int, task: object) -> _Reservation:
        b = self.balance
        total = self.context_limit if b.total is not None else None
        output = min(requested, b.output, total if total is not None else requested)
        remaining = b.total - total if b.total is not None and total is not None else None
        ticket = _Reservation(self, output, total, thread, task)
        self._persist(replace(b, output=b.output - output, total=remaining, attempts=b.attempts + 1, unconfirmed=b.unconfirmed + 1))
        self._in_flight.add(ticket)
        return ticket

    def settle(self, ticket: _Reservation, prompt: object, completion: object, total: object) -> None:
        """服务端未回传可用用量时保留整次预留, 不复用可能已消费的额度."""
        counts = _token_counts(prompt, completion, total)
        if counts is None:
            self.abandon(ticket)
            return
        with self._condition:
            if ticket not in self._in_flight:
                return
            try:
                self._settle(ticket, *counts)
            finally:
                self._release(ticket)

    def _settle(self, ticket: _Reservation, prompt: int, completion: int, total: int) -> None:
        b = self.balance
        remaining_total = b.total
        if remaining_total is not None and ticket.total is not None:
            remaining_total = max(0, remaining_total + ticket.total - total)
        self._persist(
            replace(
                b,
                output=max(0, b.output + ticket.output - completion),
                total=remaining_total,
                input_used=b.input_used + prompt,
                output_used=b.output_used + completion,
                unconfirmed=b.unconfirmed - 1,
            )
        )
        if completion > ticket.output or (ticket.total is not None and total > ticket.total):
            self.exhausted = "Model usage exceeded the declared request limit; workflow archived"
            raise ModelBudgetExhaustedError(self.exhausted)

    def abandon(self, ticket: _Reservation) -> None:
        """结束在途占用, 保留已持久化的无回执额度."""
        with self._condition:
            if ticket in self._in_flight:
                self._release(ticket)

    def _release(self, ticket: _Reservation) -> None:
        self._in_flight.remove(ticket)
        if self._temporarily_reserved():
            return
        self._condition.notify_all()
        for ready, loop in self._async_waiters.items():
            # 异步等待者可位于另一线程的事件循环, 或已随被关闭的循环结束.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(ready.set)
        self._async_waiters.clear()


_active: ContextVar[RequestBudget | None] = ContextVar("shader_request_budget", default=None)


@contextmanager
def meter_requests(meter: RequestBudget) -> Iterator[None]:
    """将额度关联到当前阶段, 子请求继承同一个线程安全账本."""
    token = _active.set(meter)
    try:
        yield
    finally:
        _active.reset(token)


def budget_active() -> bool:
    """判断本次构造的模型是否使用逐请求预算."""
    return _active.get() is not None


def _positive(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass
class _RequestReceipt:
    asynchronous: bool
    streaming: bool
    ticket: _Reservation | None = None
    counts: tuple[int, int, int] | None = None


class BudgetChatOpenAI(OpenRouterChatOpenAI):
    """为应用的 Chat Completions 请求加上真实传输额度与用量回执."""

    _receipt: _RequestReceipt | None = PrivateAttr(default=None)

    @contextmanager
    def _metered_request(self, *, asynchronous: bool = False, streaming: bool = False) -> Iterator[BudgetChatOpenAI]:
        if not budget_active():
            yield self
            return
        # 连接保持共享, 每个物理请求独占预留及最后的有效回执.
        request = self.model_copy()
        receipt = _RequestReceipt(asynchronous, streaming)
        request._receipt = receipt
        try:
            yield request
            if receipt.ticket is not None and receipt.counts is not None:
                receipt.ticket.meter.settle(receipt.ticket, *receipt.counts)
        finally:
            if receipt.ticket is not None:
                receipt.ticket.meter.abandon(receipt.ticket)

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: CallbackManagerForLLMRun | None = None, **kwargs: object
    ) -> ChatResult:
        with self._metered_request() as request:
            return super(BudgetChatOpenAI, request)._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: object
    ) -> ChatResult:
        while True:
            try:
                with self._metered_request(asynchronous=True) as request:
                    return await super(BudgetChatOpenAI, request)._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except _BudgetBusyError as error:
                await error.meter._await_capacity()

    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: CallbackManagerForLLMRun | None = None, **kwargs: object
    ) -> Iterator[ChatGenerationChunk]:
        with self._metered_request(streaming=True) as request:
            yield from super(BudgetChatOpenAI, request)._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: object
    ) -> AsyncIterator[ChatGenerationChunk]:
        while True:
            try:
                with self._metered_request(asynchronous=True, streaming=True) as request:
                    async for chunk in super(BudgetChatOpenAI, request)._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                        yield chunk
            except _BudgetBusyError as error:
                await error.meter._await_capacity()
            else:
                return

    def _get_request_payload(self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: object) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        meter = _active.get()
        receipt = self._receipt
        if meter is None or receipt is None:
            return payload
        extra = dict(payload.get("extra_body") or {})
        limits = [source.get(key) for source in (payload, extra) for key in ("max_tokens", "max_completion_tokens")]
        requested = min((value for value in limits if _positive(value)), default=_DEFAULT_REQUEST_OUTPUT)
        if payload.get("n", 1) != 1 or extra.get("n", 1) != 1 or self._use_responses_api(payload):
            msg = "Workflow budgets require one Chat Completions response per request"
            raise ValueError(msg)
        ticket = receipt.ticket = meter.reserve(requested, asynchronous=receipt.asynchronous)
        for key in ("max_tokens", "max_completion_tokens"):
            payload.pop(key, None)
            extra.pop(key, None)
        if self.model_name.lower().startswith("deepseek") or is_openrouter(self.openai_api_base):
            extra["max_tokens"] = ticket.output
        else:
            payload["max_completion_tokens"] = ticket.output
        if extra or "extra_body" in payload:
            payload["extra_body"] = extra
        if payload.get("stream"):
            payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
        return payload

    def _create_chat_result(self, response: dict | BaseModel, generation_info: dict | None = None) -> ChatResult:
        data = response if isinstance(response, dict) else response.model_dump()
        self._record_usage(data.get("usage"))
        return super()._create_chat_result(response, generation_info)

    def _record_usage(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        counts = _token_counts(raw.get("prompt_tokens"), raw.get("completion_tokens"), raw.get("total_tokens"))
        if counts is None:
            return
        receipt = self._receipt
        if receipt is None or receipt.ticket is None:
            return
        if receipt.streaming:
            receipt.counts = counts
        else:
            ticket = receipt.ticket
            ticket.meter.settle(ticket, *counts)

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        choices = chunk.get("choices")
        if choices == [] or (choices and choices[0].get("finish_reason")):
            self._record_usage(chunk.get("usage"))
        return super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)


class _InjectedModel(BaseChatModel):
    """让调用方注入的 LangChain 模型也逐次收到本工作流分配的输出上限."""

    source: BaseChatModel
    bound: Runnable | None = None

    @property
    def _llm_type(self) -> str:
        return self.source._llm_type

    def bind_tools(self, tools: Sequence, *, tool_choice: str | None = None, **kwargs: object) -> Runnable:
        bound = self.source.bind_tools(tools, tool_choice=tool_choice, **kwargs)
        return self.model_copy(update={"bound": bound})

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: CallbackManagerForLLMRun | None = None, **kwargs: object
    ) -> ChatResult:
        meter = _active.get()
        if meter is None:
            msg = "Injected workflow model must run inside its request budget"
            raise RuntimeError(msg)
        requested = kwargs.get("max_tokens")
        ticket = meter.reserve(requested if isinstance(requested, int) and requested > 0 else _DEFAULT_REQUEST_OUTPUT)
        arguments = {**kwargs, "max_tokens": ticket.output}
        runnable = self.bound or self.source
        config: RunnableConfig = {"metadata": {"workflow_parent_model_run": str(run_manager.run_id)}} if run_manager else {}
        try:
            message = runnable.invoke(messages, config=config, stop=stop, **arguments)
            if not isinstance(message, AIMessage):
                msg = "Injected workflow model returned an invalid response"
                raise TypeError(msg)
            usage = message.usage_metadata
            if usage is not None:
                meter.settle(ticket, usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"))
        finally:
            meter.abandon(ticket)
        return ChatResult(generations=[ChatGeneration(message=message)])


def workflow_model(model: BaseChatModel | None) -> BaseChatModel | None:
    """保留所注入的模型与连接, 对真实 SDK 禁用不可单独记账的内部重试."""
    if model is None or isinstance(model, _InjectedModel):
        return model
    if not isinstance(model, ChatOpenAI):
        return _InjectedModel(source=model)
    sync = model.root_client.with_options(max_retries=0)
    asynchronous = model.root_async_client.with_options(max_retries=0)
    guarded = BudgetChatOpenAI.model_construct(**model.__dict__)
    # 普通客户端可能没有私有字段, 保留预算适配器自己的逐请求回执槽.
    guarded.__pydantic_private__ = {**(guarded.__pydantic_private__ or {}), **(model.__pydantic_private__ or {})}
    return guarded.model_copy(
        update={
            "root_client": sync,
            "client": sync.chat.completions,
            "root_async_client": asynchronous,
            "async_client": asynchronous.chat.completions,
            "max_retries": 0,
        }
    )
