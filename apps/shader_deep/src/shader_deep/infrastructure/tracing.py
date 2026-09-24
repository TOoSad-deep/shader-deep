"""本地执行事件的线程安全输出适配."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.runtime.events import EventCallback
    from shader_deep.runtime.execution import AnalysisExecution


class EventLog:
    """独立锁保护事件追加, 不获取协调器的批次锁或修改黑板."""

    def __init__(self, directory: Path) -> None:
        """绑定运行目录.

        Args:
            directory: 已建立的本次运行目录.
        """
        self.path = directory / "events.jsonl"
        self.lock = Lock()

    def bind(self, task_id: str, role: str, execution: AnalysisExecution) -> EventCallback:
        """返回带任务身份和即时逻辑轮数的事件回调.

        Args:
            task_id: 当前主任务或子任务标识.
            role: 当前执行角色.
            execution: 该角色实际使用的执行计数, 不是协调器中的占位记录.

        Returns:
            仅接收简短事件详情的写入函数.
        """

        def emit(event: str, details: dict[str, JsonValue]) -> None:
            record = {
                "time": datetime.now(UTC).isoformat(),
                "task_id": task_id,
                "role": role,
                "model_call": execution.model_calls,
                "event": event,
                "details": details,
            }
            with self.lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        return emit
