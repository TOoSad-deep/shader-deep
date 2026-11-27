"""调参执行状态: 只记录真实回执, 不将模型声明当成视觉验收."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from langchain_core.messages import BaseMessage

    from shader_deep.rendering import PreviewSnapshot, ShaderPreview


@dataclass(frozen=True)
class ParameterObservation:
    """截图工具的程序回执; 图像 URL 用于证明实际内容仍在请求里."""

    snapshot: PreviewSnapshot
    image_url: str


def latest_observation(messages: Sequence[BaseMessage]) -> PreviewSnapshot | None:
    """只接受末次成功修改后的截图工具回执, 历史图不能替代当前观察."""
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        if message.name == "set_parameters":
            return None
        if message.name in {"capture_preview", "compare_preview"} and isinstance(message.artifact, ParameterObservation):
            observation = message.artifact
            expected = {"type": "image_url", "image_url": {"url": observation.image_url}}
            if isinstance(message.content, list) and expected in message.content:
                return observation.snapshot
    return None


@dataclass(frozen=True)
class ParameterProgress:
    """预算中止时的部分证据; 缺失快照不会被旧图冒充.

    Attributes:
        model_calls: 已开始的模型请求尝试数.
        parameters: 最近成功更新或截图里的完整参数; 尚无回执时为 None.
        last_snapshot: 最近成功截图, 可能早于最近参数更新.
        presented_revision: 最近一次成功模型请求中的当前截图版本; 更新后清空.
    """

    model_calls: int
    parameters: Mapping[str, float] | None
    last_snapshot: PreviewSnapshot | None
    presented_revision: int | None


class ParameterBudgetError(RuntimeError):
    """请求预算耗尽, progress 保留已获得的部分证据."""

    progress: ParameterProgress | None = None


class ParameterProtocolError(RuntimeError):
    """模型请求违反工具、顺序或最新图像检查契约."""


class ParameterControl(BaseCallbackHandler):
    """每次 runner 调用独立的预算和证据, 通过上下文传入图工作线程."""

    raise_error = True

    def __init__(self, limit: int, preview: ShaderPreview) -> None:
        self.limit = limit
        self.preview = preview
        self.calls = 0
        self.bound = False
        self.parameters: dict[str, float] | None = None
        self.last_snapshot: PreviewSnapshot | None = None
        self.presented_revision: int | None = None
        self.final_snapshot: PreviewSnapshot | None = None
        self._lock = Lock()

    @property
    def remaining(self) -> int:
        """剩余实际请求尝试额度, 包括后续重试."""
        with self._lock:
            return self.limit - self.calls

    def on_chat_model_start(self, serialized: dict[str, object], messages: list[list[BaseMessage]], **kwargs: object) -> None:
        """请求开始前计数, SDK 内部重试由模型适配器关闭."""
        del serialized, messages, kwargs
        with self._lock:
            if self.calls >= self.limit:
                msg = f"Parameter model request limit reached ({self.limit}, retries included)"
                raise ParameterBudgetError(msg)
            self.calls += 1

    def on_tool_end(self, output: object, **kwargs: object) -> None:
        """只收集成功工具回执, 不在异常收尾时调用浏览器."""
        del kwargs
        if not isinstance(output, ToolMessage) or output.status == "error":
            return
        with self._lock:
            if isinstance(output.artifact, ParameterObservation):
                self.last_snapshot = output.artifact.snapshot
                self.parameters = dict(self.last_snapshot.parameters)
            elif output.name == "set_parameters" and isinstance(output.content, str):
                self.parameters = json.loads(output.content)["parameters"]
                self.presented_revision = None

    def progress(self) -> ParameterProgress:
        """复制部分证据, 参数不随之后的运行而改变."""
        with self._lock:
            parameters = None if self.parameters is None else MappingProxyType(dict(self.parameters))
            return ParameterProgress(self.calls, parameters, self.last_snapshot, self.presented_revision)


current_control: ContextVar[ParameterControl | None] = ContextVar("parameter_run_control", default=None)
