"""固定 V0 的只读目录与对象读取, 不开放任意文件系统."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage

    from shader_deep.domain.five_libraries import FiveLibraries
    from shader_deep.domain.five_libraries.models import FrozenModel


MAX_READ_OBJECTS = 12


class LibraryReader:
    """已读集合记录真正呈现的对象, 重复读取不构成新进展."""

    def __init__(self, libraries: FiveLibraries) -> None:
        """绑定一个不可变库版本."""
        self.libraries = libraries
        self.index: dict[str, FrozenModel] = {}
        self.kinds: dict[str, str] = {}
        self.read_ids: set[str] = set()
        self.returned_ids: set[str] = set()
        for kind in ("elements", "features", "relations", "mechanisms", "sketches"):
            for item in getattr(libraries, kind):
                self.index[item.id], self.kinds[item.id] = item, kind

    def directory(self) -> dict[str, object]:
        """只暴露身份、名称与规模, 完整语义按需读取."""
        return {
            kind: [{"id": identity, "name": getattr(item, "name", None)} for identity, item in self.index.items() if self.kinds[identity] == kind]
            for kind in ("elements", "features", "relations", "mechanisms", "sketches")
        }

    def mark_presented(self, messages: list[BaseMessage]) -> None:
        """正文实际进入后续模型请求后才允许语义合并."""
        for message in messages:
            if isinstance(message, ToolMessage) and isinstance(message.content, str):
                try:
                    payload = json.loads(message.content)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    self.read_ids.update(set(payload) & self.returned_ids)

    def tools(self) -> list[BaseTool]:
        """每次工具仅读取明确 ID, 不接受目录路径或兄弟运行文件."""

        @tool
        def read_library_objects(ids: list[str]) -> str:
            """读取固定 V0 对象正文; 合并前必须读取每个对象.

            Args:
                ids: 此版本目录中的明确对象 ID, 最多十二个.
            """
            if not ids or len(ids) > MAX_READ_OBJECTS or any(identity not in self.index for identity in ids):
                msg = "提供一到十二个固定 V0 中存在的对象 ID"
                raise ValueError(msg)
            self.returned_ids.update(ids)
            return json.dumps({identity: self.index[identity].model_dump(mode="json") for identity in ids}, ensure_ascii=False)

        return [read_library_objects]
