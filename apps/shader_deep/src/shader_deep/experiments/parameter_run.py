"""有请求预算和完整追踪边界的调参执行入口, 浏览器仍由调用方管理."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.runnables.config import merge_configs
from langsmith import traceable

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph

    from shader_deep.rendering import PreviewSnapshot, ShaderPreview


class ParameterBudgetError(RuntimeError):
    """实际模型请求尝试次数耗尽, 包括建连失败和重试."""


@dataclass(frozen=True)
class ParameterRunResult:
    """图正常结束且收尾取图成功的结果, 不代表视觉匹配通过验收.

    Attributes:
        messages: 本次图返回的完整消息, 保留工具回执及实际图像.
        snapshot: 图结束后重新获取的当前渲染.
        model_calls: 含失败和重试的实际模型请求尝试次数.
    """

    messages: tuple[BaseMessage, ...]
    snapshot: PreviewSnapshot
    model_calls: int


class _RequestBudget(BaseCallbackHandler):
    raise_error = True

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls = 0
        self._lock = Lock()

    def on_chat_model_start(self, serialized: dict[str, object], messages: list[list[BaseMessage]], **kwargs: object) -> None:
        del serialized, messages, kwargs
        with self._lock:
            if self.calls >= self.limit:
                msg = f"Parameter model request limit reached ({self.limit}, retries included)"
                raise ParameterBudgetError(msg)
            self.calls += 1


@traceable(name="shader-deep.parameter-run")
def run_parameter_task(
    agent: CompiledStateGraph,
    task: HumanMessage,
    preview: ShaderPreview,
    *,
    max_model_calls: int = 20,
    config: RunnableConfig | None = None,
) -> ParameterRunResult:
    """执行已构建的图, 将模型失败、预算及最终取图纳入同一追踪.

    Args:
        agent: build_parameter_agent 创建的图.
        task: 已冻结参考图或普通文字任务.
        preview: 调用方在 with 内持有的同一会话.
        max_model_calls: 实际模型请求尝试总上限, 重试也占用.
        config: 额外回调、图步数或追踪配置, 不替换内置计数回调.

    Returns:
        完整消息、最新快照和请求尝试次数.

    Raises:
        ValueError: 请求预算不是正整数.
        ParameterBudgetError: 请求预算耗尽.
        RuntimeError: 图未给出完整最终回复.
        Exception: 模型请求、工具或最终取图失败时的原始异常.
    """
    if isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls < 1:
        msg = "max_model_calls must be a positive integer"
        raise ValueError(msg)
    counter = _RequestBudget(max_model_calls)
    settings = merge_configs({"recursion_limit": 80}, config, {"callbacks": [counter]})
    result = agent.invoke({"messages": [task]}, config=settings)
    messages = tuple(result["messages"])
    final = messages[-1] if messages else None
    if not isinstance(final, AIMessage) or final.tool_calls or not final.content:
        msg = "Parameter agent ended without a final response"
        raise RuntimeError(msg)
    # 只在图成功后取图. 不在 finally 渲染, 避免覆盖模型/工具的原始异常.
    return ParameterRunResult(messages=messages, snapshot=preview.capture(), model_calls=counter.calls)
