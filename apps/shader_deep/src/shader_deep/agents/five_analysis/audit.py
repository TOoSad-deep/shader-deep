"""五库角色保留请求恢复、用量与原始工具交互的运行审计."""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage

from shader_deep.infrastructure.analysis_logging import log_analysis, submission_summary
from shader_deep.infrastructure.storage.journal import append_tool_record

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from pydantic import JsonValue

    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.runtime.task_store import Attempt, TaskStore

AUDIT_LOCK = Lock()


class RoleAudit:
    """审计只记录发生过的事件, 不作为第二个任务状态来源."""

    def __init__(self, store: TaskStore, attempt: Attempt, execution: AnalysisExecution) -> None:
        """绑定独立运行与执行实例身份."""
        self.directory, self.execution = store.directory, execution
        self.identity = {"run_id": store.directory.name, "task_id": attempt.task_id, "attempt_id": attempt.attempt_id}

    def event(self, event: str, details: dict[str, JsonValue]) -> None:
        """状态成功提交后发出的事件不会决定完成判定."""
        record = {**self.identity, "event": event, "model_call": self.execution.model_calls, **details}
        with AUDIT_LOCK, (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._event_log(event, details)

    def _log(self, message: str, *, level: int = logging.INFO, details: dict[str, JsonValue] | None = None) -> None:
        log_analysis(
            self.directory,
            message,
            task_id=self.identity["task_id"],
            attempt_id=self.identity["attempt_id"],
            model_call=self.execution.model_calls,
            level=level,
            details=details,
        )

    def _event_log(self, event: str, details: dict[str, JsonValue]) -> None:
        """展示关键节点; 完整结构事件继续保存在 events.jsonl."""
        labels = {
            "task_started": "角色开始执行",
            "model_started": "模型调用开始",
            "request_started": "网络请求开始",
            "request_completed": "网络请求完成",
            "request_failed": "网络请求失败",
            "request_retry": "准备网络重试",
            "model_usage": "模型 token 用量",
            "tool_rejected": "提交校验拒绝",
            "task_completed": "角色已提交结果",
        }
        if event in labels:
            level = logging.WARNING if event in {"request_failed", "tool_rejected"} else logging.INFO
            self._log(labels[event], level=level, details=details)
        elif event == "request_sizes":
            self._log("请求材料统计", level=logging.DEBUG, details=details)

    def tool(self, record: dict[str, JsonValue]) -> None:
        """完整提交参数与拒绝诊断保持来源身份, 不回注探索材料."""
        append_tool_record(self.directory, AUDIT_LOCK, self.execution.model_calls, {**self.identity, **record})
        tool, outcome = str(record.get("tool")), str(record.get("outcome"))
        level = logging.INFO if outcome == "success" else logging.WARNING
        self._log(f"工具提交 {tool}: {outcome}\n" + submission_summary(tool, record.get("arguments")), level=level)
        self._log("完整业务提交与回执", level=logging.DEBUG, details=record)

    def history(self, messages: list[BaseMessage]) -> None:
        """展示模型响应概况, 不记录图片、推理块或完整请求历史."""
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "\n".join(
                    block if isinstance(block, str) else str(block.get("text", ""))
                    for block in content
                    if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
                )
            )
            self._log(
                "模型响应(尚未执行工具校验)",
                details={
                    "finish_reason": message.response_metadata.get("finish_reason"),
                    "text_chars": len(text),
                    "tools": [call["name"] for call in message.tool_calls],
                },
            )
            if text:
                self._log("模型正文", level=logging.DEBUG, details={"text": text})
