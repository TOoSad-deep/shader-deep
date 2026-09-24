"""追加完整工具交互, 不向模型上下文回注日志."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _thread import LockType
    from pathlib import Path

    from pydantic import JsonValue


def append_tool_record(directory: Path, lock: LockType, model_call: int, record: dict[str, JsonValue]) -> None:
    """在运行目录中串行保存一条工具交互."""
    with lock, (directory / "tools.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"model_call": model_call, **record}, ensure_ascii=False) + "\n")
