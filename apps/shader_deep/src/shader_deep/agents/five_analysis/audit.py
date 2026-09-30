"""五库角色保留请求恢复、用量与原始工具交互的运行审计."""

from __future__ import annotations

import json
from threading import Lock
from typing import TYPE_CHECKING

from shader_deep.infrastructure.storage.journal import append_tool_record

if TYPE_CHECKING:
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

    def tool(self, record: dict[str, JsonValue]) -> None:
        """完整提交参数与拒绝诊断保持来源身份, 不回注探索材料."""
        append_tool_record(self.directory, AUDIT_LOCK, self.execution.model_calls, {**self.identity, **record})
