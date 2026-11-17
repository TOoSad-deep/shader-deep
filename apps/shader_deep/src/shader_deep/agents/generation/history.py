"""将已归档工具历史投影为本轮请求, 不改写 SDK 保存的执行消息."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from langchain.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from langchain_core.messages import BaseMessage

MessageContent = str | list[str | dict[str, object]]


@dataclass(frozen=True, kw_only=True)
class RenderHistoryReference:
    """一次已执行渲染对应的固定材料, 不包含历史代码或错误正文.

    Attributes:
        candidate_id: 生成侧登记的候选身份.
        code_material_id: 已保存完整代码的材料身份.
        result_material_id: 已保存单次执行结果及错误的材料身份.
        status: 渲染动作的成功或失败, 不代表业务完成或视觉验收.
        preview_material_id: 成功渲染的完整预览材料, 失败时为空.
    """

    candidate_id: str
    code_material_id: str
    result_material_id: str
    status: Literal["rendered", "render_error"]
    preview_material_id: str | None = None

    def receipt(self) -> str:
        """返回供历史回看的简短响应, 原错误通过结果材料读取."""
        payload = {
            "status": self.status,
            "candidate_id": self.candidate_id,
            "code_material_id": self.code_material_id,
            "result_material_id": self.result_material_id,
        }
        if self.preview_material_id is not None:
            payload["preview_material_id"] = self.preview_material_id
        return json.dumps(payload, ensure_ascii=False)

    def code_reference(self) -> str:
        """明确说明历史参数仅为引用, 防止被当成可执行 GLSL."""
        return f"[历史代码正文已移出; 使用 read_materials 读取 {self.code_material_id}; 此引用不是可执行 GLSL]"


def _completed_calls(messages: Sequence[BaseMessage], name: str) -> set[str]:
    """仅选择同时存在声明调用和对应响应的工具身份."""
    responded = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    called = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == name and call["id"] is not None
    }
    return called & responded


def _raw_reference(call: dict[str, object], references: Mapping[str, RenderHistoryReference]) -> None:
    """处理适配器保存的 OpenAI 参数副本和已解析原文副本."""
    identity = call.get("id")
    reference = references.get(identity) if isinstance(identity, str) else None
    if reference is None:
        return
    arguments = json.dumps({"glsl_code": reference.code_reference()}, ensure_ascii=False)
    function = call.get("function")
    if isinstance(function, dict) and function.get("name") == "render_shader":
        function["arguments"] = arguments
    elif call.get("name") == "render_shader":
        call["args"] = arguments if isinstance(call.get("args"), str) else {"glsl_code": reference.code_reference()}


def _project_call(message: AIMessage, references: Mapping[str, RenderHistoryReference]) -> None:
    """只修改深拷贝中的已归档参数, 保留推理和未执行调用."""
    for call in message.tool_calls:
        reference = references.get(call["id"] or "")
        if call["name"] == "render_shader" and reference is not None:
            call["args"] = {"glsl_code": reference.code_reference()}
    # 原文参数可能被不同模型适配器读取, 必须与标准 tool_calls 一致.
    for key in ("tool_calls", "analysis_raw_tool_calls"):
        raw = message.additional_kwargs.get(key)
        if isinstance(raw, list):
            for call in raw:
                if isinstance(call, dict):
                    _raw_reference(call, references)


def project_history(
    messages: Sequence[BaseMessage],
    *,
    rendered_calls: Mapping[str, RenderHistoryReference],
    read_contents: Mapping[str, MessageContent] | None = None,
) -> list[BaseMessage]:
    """生成本轮消息副本, 归档渲染正文并临时展开指定读取响应.

    Args:
        messages: SDK 保存的原始消息, 读取响应应已为简短回执.
        rendered_calls: 已成功登记材料的渲染调用, 以工具调用 ID 定位.
        read_contents: 本轮需要展开的读取响应正文, 由调用方完成选材和去重.

    Returns:
        独立的请求消息副本, 不改变原始消息、调用配对或执行参数.
    """
    completed = _completed_calls(messages, "render_shader")
    references = {identity: reference for identity, reference in rendered_calls.items() if identity in completed}
    reads = _completed_calls(messages, "read_materials")
    copies = [message.model_copy(deep=True) for message in messages]
    for message in copies:
        if isinstance(message, AIMessage):
            _project_call(message, references)
        elif isinstance(message, ToolMessage):
            if reference := references.get(message.tool_call_id):
                message.content = reference.receipt()
            elif read_contents is not None and message.tool_call_id in reads and message.tool_call_id in read_contents:
                message.content = deepcopy(read_contents[message.tool_call_id])
    return copies
