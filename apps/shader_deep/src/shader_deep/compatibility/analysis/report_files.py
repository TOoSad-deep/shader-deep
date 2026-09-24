"""将不可变子报告导出为只读材料, 按实际工具正文记录 JSON 对象覆盖."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from deepagents.backends.filesystem import FilesystemBackend
from langchain.messages import ToolMessage

from shader_deep.domain.references import report_catalog
from shader_deep.runtime.submissions.handler import resolve_pointer

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.messages import BaseMessage

    from shader_deep.domain.legacy import SourceRef
    from shader_deep.domain.tasks import ResultRecord

# SDK 默认在 20,000 * 4 字符后外置工具正文; 在完整回执上留出余量.
MAX_REPORT_RESPONSE_CHARS = 76_000


def _child_pointer(pointer: str, key: str | int) -> str:
    """编码单个 JSON Pointer 路径片段."""
    return pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _covered(value: object, pointer: str, presented: set[str]) -> bool:
    """完整父对象或全部子项均已呈现时, 当前对象才算已读."""
    if any(pointer == item or pointer.startswith(item + "/") for item in presented):
        return True
    if isinstance(value, dict) and value:
        return all(_covered(child, _child_pointer(pointer, key), presented) for key, child in value.items())
    if isinstance(value, list) and value:
        return all(_covered(child, _child_pointer(pointer, index), presented) for index, child in enumerate(value))
    return False


def _body_entries(section: dict[str, object], pointer: str) -> list[tuple[str, object]]:
    """固定按完整业务条目计数, 不因读取父章节或子字段改变计量单位."""
    entries: list[tuple[str, object]] = []
    for key, value in section.items():
        location = _child_pointer(pointer, key)
        if key == "kind" or not value:
            continue
        if isinstance(value, list):
            entries.extend((_child_pointer(location, index), item) for index, item in enumerate(value))
        elif isinstance(value, dict):
            entries.extend(_body_entries(value, location))
        else:
            entries.append((location, value))
    return entries


class AnalysisReportFiles:
    """管理报告材料副本及其实际进入主模型请求的范围."""

    def __init__(self, directory: Path) -> None:
        """将 Backend 的虚拟根限制在本次运行的报告目录."""
        self.directory = directory / "reports"
        self.directory.mkdir(exist_ok=True)
        self.backend = FilesystemBackend(root_dir=self.directory, virtual_mode=True)
        self.results: dict[str, ResultRecord] = {}
        self.documents: dict[str, object] = {}
        self.presented: dict[str, set[str]] = {}
        self.with_body: set[str] = set()
        self.pending: dict[str, tuple[str, str, str]] = {}

    def register(self, result: ResultRecord) -> None:
        """只导出一次已登记报告; 材料副本不作为可编辑业务状态."""
        if result.id in self.results:
            return
        payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
        with (self.directory / f"{result.id}.json").open("x", encoding="utf-8") as stream:
            stream.write(payload)
        self.results[result.id] = result
        self.documents[result.id] = json.loads(payload)
        self.presented[result.id] = set()

    def catalog(self) -> list[dict[str, object]]:
        """复用来源身份目录, 只返回定位字段而不重复展开报告正文."""
        return [
            {
                "result_id": entry["result_id"],
                "item_id": entry["item_id"],
                "kind": entry["kind"],
                "file_path": f"/{result.id}.json",
                "pointer": "/analysis_detail" + entry["pointer"] if entry["pointer"] else "",
            }
            for result in self.results.values()
            for entry in report_catalog(result)
        ]

    def _locations(self, identifier: str) -> list[str]:
        """大对象被拒绝时给出实际章节与条目位置."""
        document = cast("dict[str, object]", self.documents[identifier])
        detail = cast("dict[str, object]", document["analysis_detail"])
        return [
            *(_child_pointer("", key) for key in document),
            *(_child_pointer("/analysis_detail", key) for key in detail),
            *(cast("str", entry["pointer"]) for entry in self.catalog() if entry["result_id"] == identifier and entry["item_id"] is not None),
        ]

    def read(self, file_path: str, pointer: str, tool_call_id: str) -> str:
        """通过 Backend 读取明确登记的报告, 成功正文等待下一轮请求确认."""
        identifier = next((item for item in self.results if file_path == f"/{item}.json"), None)
        if identifier is None:
            msg = "Choose a file_path from report_files in the current context"
            raise ValueError(msg)
        response = self.backend.download_files([file_path])[0]
        if response.error or response.content is None:
            msg = f"Report file could not be read: {response.error}"
            raise ValueError(msg)
        document = json.loads(response.content.decode("utf-8"))
        value = resolve_pointer(document, pointer)
        payload = json.dumps(
            {"status": "read", "result_id": identifier, "file_path": file_path, "pointer": pointer, "complete": True, "content": value},
            ensure_ascii=False,
        )
        if len(payload) > MAX_REPORT_RESPONSE_CHARS:
            return json.dumps(
                {
                    "status": "too_large",
                    "complete": False,
                    "message": "Read smaller sections or individual entries",
                    "pointers": self._locations(identifier),
                },
                ensure_ascii=False,
            )
        self.pending[tool_call_id] = (identifier, pointer, payload)
        return payload

    def on_prepared(self, messages: list[BaseMessage]) -> int:
        """只接受原调用 ID 下未被截断或外置的完整工具正文."""
        changed = 0
        for message in messages:
            if not isinstance(message, ToolMessage) or message.tool_call_id not in self.pending:
                continue
            identifier, pointer, body = self.pending[message.tool_call_id]
            if message.status == "error" or message.content != body:
                continue
            value = resolve_pointer(self.documents[identifier], pointer)
            if _covered(value, pointer, self.presented[identifier]):
                del self.pending[message.tool_call_id]
                continue
            self.presented[identifier].add(pointer)
            changed += 1
            if self._has_body(identifier):
                self.with_body.add(identifier)
            del self.pending[message.tool_call_id]
        return changed

    def _has_body(self, identifier: str) -> bool:
        """至少有一项完整正文, 只读取 kind 等结构标记不算阅读报告."""
        detail = cast("dict[str, object]", resolve_pointer(self.documents[identifier], "/analysis_detail"))
        presented = self.presented[identifier]
        for key, value in detail.items():
            pointer = _child_pointer("/analysis_detail", key)
            if key == "kind" or not value:
                continue
            if _covered(value, pointer, presented):
                return True
            if isinstance(value, list) and any(_covered(item, _child_pointer(pointer, index), presented) for index, item in enumerate(value)):
                return True
        return False

    def body_count(self) -> int:
        """从既有覆盖记录计算完整正文条目数, 元数据和重叠读取不增加进展.

        Returns:
            已完整进入模型请求的业务正文条目数量.
        """
        return sum(
            _covered(value, pointer, self.presented[identifier])
            for identifier, document in self.documents.items()
            for pointer, value in _body_entries(cast("dict[str, object]", resolve_pointer(document, "/analysis_detail")), "/analysis_detail")
        )

    def has_source(self, reference: SourceRef) -> bool:
        """具体来源按条目覆盖判断; 整报告需要完整 JSON 树均已呈现."""
        document = self.documents.get(reference.result_id)
        if document is None:
            return False
        pointer = next(
            (
                cast("str", item["pointer"])
                for item in self.catalog()
                if item["result_id"] == reference.result_id and item["item_id"] == reference.item_id
            ),
            None,
        )
        if pointer is None:
            return False
        return _covered(resolve_pointer(document, pointer), pointer, self.presented[reference.result_id])
