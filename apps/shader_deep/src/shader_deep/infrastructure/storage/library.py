"""四库文件持久化与旧消息呈现入口的适配."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import ToolMessage

from shader_deep.domain.library.store import MAX_LIBRARY_RESPONSE_CHARS, LibraryStore as BusinessLibraryStore

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.messages import BaseMessage

    from shader_deep.domain.library.models import VisualOutline


class LibraryFiles:
    """单个运行中的版本文件与不可变源报告."""

    def __init__(self, directory: Path) -> None:
        """创建当前运行的四库制品目录."""
        self.directory = directory / "possibility_library"
        self.directory.mkdir(parents=True, exist_ok=True)

    def save_version(self, revision: int, snapshot: dict[str, object]) -> None:
        """先写完整临时文件再替换; 写入失败不发布业务版本."""
        destination = self.directory / f"version-{revision:04d}.json"
        if destination.exists():
            msg = "Library version already exists; use a fresh run directory"
            raise ValueError(msg)
        temporary = destination.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def save_source(self, report_id: str, report: dict[str, object]) -> None:
        """排他创建原报告; 同内容重试保持幂等."""
        destination = self.directory / f"source-{report_id}.json"
        body = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        if destination.exists():
            if destination.read_text(encoding="utf-8") == body:
                return
            msg = f"Immutable source differs: {report_id}"
            raise ValueError(msg)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(body)


class FileLibraryStore(BusinessLibraryStore):
    """绑定文件存储的业务库; 保留既有目录构造方式."""

    def __init__(self, outline: VisualOutline, directory: Path, *, max_response_chars: int = MAX_LIBRARY_RESPONSE_CHARS) -> None:
        """将目录转换为持久化适配器, 业务校验留在父类."""
        files = LibraryFiles(directory)
        super().__init__(outline, files, max_response_chars=max_response_chars)
        self.directory = files.directory

    def on_prepared(self, messages: list[BaseMessage]) -> int:
        """将真实工具消息转换为业务层可校验的呈现回执."""
        return self.present_tool_results(
            [(message.tool_call_id, message.content, message.status == "error") for message in messages if isinstance(message, ToolMessage)]
        )
