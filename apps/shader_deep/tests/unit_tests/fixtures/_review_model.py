"""独立评审真实 SDK 图使用的无网络模型与消息检查."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from shader_deep.domain.visual_review import BoundReviewCriteria
from tests.unit_tests.fixtures._review_fixture import matched_review

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.language_models import LanguageModelInput
    from langchain_core.runnables import Runnable


def review_call(name: str, arguments: dict[str, object], identity: str = "call") -> AIMessage:
    """构造单次工具提交消息."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identity}])


def review_context(messages: list[BaseMessage]) -> dict[str, object]:
    """从实际模型请求中定位固定评审上下文."""
    for message in reversed(messages):
        if not isinstance(message, HumanMessage) or not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    payload = json.loads(str(block["text"]))
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("kind") == "visual_review_context":
                    return payload
    msg = "实际模型请求中缺少固定评审输入"
    raise AssertionError(msg)


class ReviewModel(BaseChatModel):
    """捕获最终模型请求, 默认提交符合本轮范围的匹配报告."""

    responses: list[AIMessage] = Field(default_factory=list)
    requests: list[list[BaseMessage]] = Field(default_factory=list)
    offered_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "visual-review-offline-test"

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Runnable[LanguageModelInput, BaseMessage]:
        del kwargs
        self.offered_tools = [str(getattr(item, "name", "")) for item in tools]
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object) -> ChatResult:
        del stop, run_manager, kwargs
        self.requests.append(list(messages))
        index = len(self.requests) - 1
        if index < len(self.responses):
            response = self.responses[index]
        else:
            criteria = BoundReviewCriteria.model_validate(review_context(messages)["criteria"])
            response = review_call("submit_review", {"report": matched_review(criteria).model_dump(mode="json")}, str(index))
        return ChatResult(generations=[ChatGeneration(message=response)])
