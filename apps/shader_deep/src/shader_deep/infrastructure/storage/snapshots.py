"""单文件快照写入, 不声明跨文件事务或模型会话恢复."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def write_snapshot(destination: Path, payload: object, *, temporary_suffix: str = ".tmp") -> None:
    """先完整写入同目录临时文件, 再替换目标文件.

    Args:
        destination: 已选择且父目录存在的目标路径.
        payload: 可序列化的业务快照.
        temporary_suffix: 保留既有各制品的临时文件命名.
    """
    temporary = destination.with_suffix(temporary_suffix)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
