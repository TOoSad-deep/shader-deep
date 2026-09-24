"""仅压缩已消费工具回执, 保留工具调用配对和当前修复证据."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from langchain_core.messages import BaseMessage

QUERY_TOOLS = {"list_library", "list_comparison_work", "list_analysis_context"}
READ_TOOLS = {"read_library"}
DRAFT_TOOLS = {"submit_integration", "repair_analysis_submission"}


def _payload(message: ToolMessage) -> dict[str, object]:
    """普通文本或未知回执不作为可压缩的成功读取."""
    try:
        value = json.loads(message.content) if isinstance(message.content, str) else None
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _receipt(name: str, payload: dict[str, object]) -> str:
    """索引正文只保留计数和短游标, 原文由工具制品负责保存."""
    summary: dict[str, object] = {"history_compacted": True, "tool": name, "status": payload.get("status", "consumed")}
    for key in ("revision", "total", "remaining", "next_cursor", "reason"):
        value = payload.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            summary[key] = value if not isinstance(value, str) else value[:256]
    for key in ("entries", "selected_ids", "duplicate_ids", "not_provided_ids"):
        values = payload.get(key)
        if isinstance(values, list):
            summary[f"{key}_count"] = len(values)
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def compact_consumed_history(
    messages: list[BaseMessage],
    consumed_call_ids: AbstractSet[str],
    *,
    replaced_draft_call_ids: AbstractSet[str] = frozenset(),
) -> list[BaseMessage]:
    """整理请求副本, 不修改原历史或把未发送结果当作已消费.

    Args:
        messages: 完整请求历史.
        consumed_call_ids: 已在先前实际请求呈现的工具响应身份.
        replaced_draft_call_ids: 当前完整有效草稿及错误已由宿主另行提供的调用身份.

    Returns:
        保留配对和失败读取回执的历史副本. 草稿仅压缩宿主明确替代的调用.
    """
    calls = {call["id"]: call for message in messages if isinstance(message, AIMessage) for call in message.tool_calls}
    replies = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
    replacements: dict[str, str] = {}
    draft_ids = {
        identity for identity in replaced_draft_call_ids if identity in calls and identity in replies and calls[identity]["name"] in DRAFT_TOOLS
    }
    for identity in consumed_call_ids & calls.keys() & replies.keys():
        name, reply = calls[identity]["name"], replies[identity]
        payload = _payload(reply)
        success = reply.status != "error" and payload.get("status") not in {"rejected", "error", "not_selected"}
        if success and (name in QUERY_TOOLS or (name in READ_TOOLS and payload.get("status") == "selected")):
            replacements[identity] = _receipt(name, payload)
    return [_compact_message(message, replacements, draft_ids) for message in messages]


def _compact_message(message: BaseMessage, replacements: dict[str, str], draft_ids: set[str]) -> BaseMessage:
    """只替换匹配的响应及明确替代的草稿参数, 不删除消息或调用身份."""
    if isinstance(message, ToolMessage):
        content = replacements.get(message.tool_call_id)
        if message.tool_call_id in draft_ids:
            content = '{"history_compacted":true,"current_draft_in_context":true}'
        return message.model_copy(update={"content": content}) if content is not None else message
    if isinstance(message, AIMessage) and any(call["id"] in draft_ids for call in message.tool_calls):
        calls = [{**call, "args": {}} if call["id"] in draft_ids else call for call in message.tool_calls]
        extra = {key: value for key, value in message.additional_kwargs.items() if key not in {"tool_calls", "analysis_raw_tool_calls"}}
        return message.model_copy(update={"tool_calls": calls, "additional_kwargs": extra})
    return message
