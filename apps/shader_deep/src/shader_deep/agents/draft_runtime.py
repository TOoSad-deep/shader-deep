"""两个专用角色共用的草稿工具、顺序执行与实际回读回执."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from threading import Event, Lock
from typing import TYPE_CHECKING, Literal, Protocol, cast

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileData, ReadResult, WriteResult
from deepagents.backends.utils import slice_read_response
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from typing_extensions import override

from shader_deep.infrastructure.storage.json_draft import DraftError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import BaseMessage
    from langgraph.runtime import Runtime
    from langgraph.types import Command
    from pydantic import JsonValue


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class DraftFile(Protocol):
    """工具及回执只读取草稿文本和当前字节版本."""

    @property
    def revision(self) -> str | None:
        """返回当前磁盘提交的字节摘要."""
        ...

    def read(self, *, expected_revision: str | None = None) -> tuple[str, str]:
        """读取同一草稿并核对可选版本."""
        ...


class DraftToolSession(Protocol):
    """共享运行辅助依赖的会话动作, 领域接受由具体角色实现."""

    request_revision: str | None

    @property
    def directory(self) -> Path:
        """返回本次运行目录."""
        ...

    @property
    def draft(self) -> DraftFile:
        """返回会话唯一草稿."""
        ...

    @property
    def finished(self) -> bool:
        """返回会话是否已结束."""
        ...

    @property
    def last_edit(self) -> dict[str, JsonValue]:
        """返回最新编辑的工具反馈."""
        ...

    def begin_model_call(self) -> bool:
        """检查终态和逻辑调用预算."""
        ...

    def record_rejection(self, *, validation: bool) -> None:
        """记录合同或准入拒绝."""
        ...

    def record_resource_read(self, path: str, status: Literal["success", "error"]) -> None:
        """记录规则文件读取状态."""
        ...

    def record_draft_read(self, identity: str, content: str) -> None:
        """登记实际工具回读正文."""
        ...

    def edit_draft(self, old: str, new: str, *, replace_all: bool = False) -> dict[str, JsonValue]:
        """按模型请求版本局部编辑."""
        ...


class DraftReadTracker:
    """只累计实际入模的同版本行范围, 由会话锁保护调用."""

    def __init__(self, draft: DraftFile, *, finish_tool: str) -> None:
        """绑定同一草稿与完成工具名称."""
        self.draft, self.finish_tool = draft, finish_tool
        self.seen_revision: str | None = None
        self._receipts: dict[str, tuple[str, str, set[int]]] = {}
        self._seen_lines: dict[str, set[int]] = {}

    def invalidate(self) -> None:
        """编辑后清除当前完整已见版本."""
        self.seen_revision = None

    def record(self, identity: str, content: str) -> None:
        """登记工具正文包含的同版本完整行区间."""
        text, revision = self.draft.read()
        header = re.search(r"@@ lines (\d+)-(\d+) of (\d+)(?: \| next offset \d+)? @@", content)
        if header is None:
            return
        start, end, total = map(int, header.groups())
        lines = text.splitlines()
        body = "\n".join(lines[start - 1 : end])
        if total == len(lines) and body and body in content:
            self._receipts[identity] = (revision, hashlib.sha256(content.encode()).hexdigest(), set(range(start - 1, end)))

    def present(self, messages: list[object]) -> None:
        """成功主请求实际包含匹配工具正文后累计已见范围."""
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            receipt = self._receipts.get(message.tool_call_id)
            if receipt is None or not isinstance(message.content, str) or hashlib.sha256(message.content.encode()).hexdigest() != receipt[1]:
                continue
            revision, _, lines = receipt
            self._seen_lines.setdefault(revision, set()).update(lines)
        if self.draft.revision is not None:
            text, revision = self.draft.read()
            if self._seen_lines.get(revision, set()) == set(range(len(text.splitlines()))):
                self.seen_revision = revision

    def unseen_message(self, text: str, revision: str) -> str:
        """区分已返回等待入模和仍需分页读取的情况."""
        total = len(text.splitlines())
        returned = {line for rev, _, lines in self._receipts.values() if rev == revision for line in lines}
        if returned == set(range(total)):
            return f"完整回读已返回, 请在下一次模型响应中再调用 {self.finish_tool}"
        seen = self._seen_lines.get(revision, set())
        offset = next((line for line in range(total) if line not in seen), 0)
        return f"最新草稿尚有未见正文, 请 read_file offset={offset}, limit={min(2000, total - offset)}, 并在后续响应完成"


class DraftBackend(FilesystemBackend):
    """仅映射 CompositeBackend 去前缀后的固定草稿文件."""

    def __init__(self, session: DraftToolSession, *, filename: str) -> None:
        """使用同一会话草稿存储, 不建立独立文件正文缓存."""
        super().__init__(root_dir=session.directory / "draft", virtual_mode=True)
        self.session = session
        self.file_path = "/" + filename

    @override
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """读取草稿的实际字节并交由 SDK 分页."""
        if file_path != self.file_path:
            return ReadResult(error="draft_path_denied")
        try:
            text, _ = self.session.draft.read()
            return slice_read_response(FileData(content=text, encoding="utf-8"), offset, limit)
        except ValueError as error:
            return ReadResult(
                error=json.dumps(
                    error.as_dict() if isinstance(error, DraftError) else {"code": "draft_error", "message": str(error)}, ensure_ascii=False
                )
            )

    @override
    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """调用会话串行编辑, SDK 工具反馈由准入层补充版本和诊断."""
        if file_path != self.file_path:
            return EditResult(error="draft_path_denied")
        try:
            self.session.edit_draft(old_string, new_string, replace_all=replace_all)
            return EditResult(path=file_path, occurrences=1)
        except ValueError as error:
            return EditResult(
                error=json.dumps(
                    error.as_dict() if isinstance(error, DraftError) else {"code": "draft_error", "message": str(error)}, ensure_ascii=False
                )
            )

    @override
    def write(self, file_path: str, content: str) -> WriteResult:
        """草稿首次创建只能经过保存工具."""
        del file_path, content
        return WriteResult(error="draft_write_denied")

    @override
    def delete(self, file_path: str) -> DeleteResult:
        """草稿后端不开放删除入口."""
        del file_path
        return DeleteResult(error="draft_delete_denied")


@dataclass
class _ToolBatch:
    """一条模型响应的顺序执行回执, 不保存模型正文."""

    key: str
    ids: tuple[str, ...]
    revision: str | None
    failed: bool = False
    sync: list[Event] = field(init=False)
    asynchronous: list[asyncio.Event] = field(init=False)

    def __post_init__(self) -> None:
        self.sync = [Event() for _ in self.ids]
        self.asynchronous = [asyncio.Event() for _ in self.ids]


class DraftToolGuard(AgentMiddleware[AgentState[object], None]):
    """在摘要包装链之前结束, 同时控制逻辑调用预算和工具准入."""

    def __init__(
        self,
        session: DraftToolSession,
        *,
        allowed_tools: frozenset[str],
        write_tools: frozenset[str],
        draft_path: str,
        resource_paths: frozenset[str] = frozenset(),
        readable_prefixes: tuple[str, ...] = (),
    ) -> None:
        """绑定本次独立业务会话."""
        self.session = session
        self.allowed_tools, self.write_tools = allowed_tools, write_tools
        self.draft_path, self.resource_paths = draft_path, resource_paths
        self.readable_prefixes = readable_prefixes
        self._batch_lock = Lock()
        self._batch: _ToolBatch | None = None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """终态先于摘要和计数, 达到预算也不发起额外模型请求."""
        del state, runtime
        return None if self.session.begin_model_call() else {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """异步路径使用同一终态门禁."""
        return self.before_model(state, runtime)

    def _readable(self, value: object) -> bool:
        if not isinstance(value, str) or "\\" in value or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
            return False
        return value == self.draft_path or value in self.resource_paths or value.startswith(self.readable_prefixes)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call["name"]
        if self.session.finished:
            reason = "会话已结束, 不再执行工具"
        elif name not in self.allowed_tools:
            reason = "本轮只允许草稿保存、回读、编辑、完成与停止"
        elif name == "read_file" and not self._readable(request.tool_call["args"].get("file_path")):
            reason = "仅可读取本角色规则、样例及本轮 SDK 外置材料"
        elif name == "edit_file" and request.tool_call["args"].get("file_path") != self.draft_path:
            reason = f"仅可编辑 {self.draft_path}"
        else:
            return None
        self.session.record_rejection(validation=False)
        return ToolMessage(content=_encoded({"error": reason}), tool_call_id=request.tool_call["id"], name=name, status="error")

    def _record_result(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        if isinstance(result, ToolMessage) and result.status == "error" and result.name in self.write_tools:
            self.session.record_rejection(validation=True)
        path = request.tool_call["args"].get("file_path")
        if request.tool_call["name"] == "read_file" and isinstance(result, ToolMessage) and path in self.resource_paths:
            self.session.record_resource_read(str(path), result.status)
        if isinstance(result, ToolMessage) and result.status == "success":
            if request.tool_call["name"] == "read_file" and path == self.draft_path:
                _, revision = self.session.draft.read()
                result = result.model_copy(update={"content": f"Draft revision: {revision}\n{result.content}"})
                self.session.record_draft_read(result.tool_call_id, str(result.content))
            elif request.tool_call["name"] == "edit_file":
                result = result.model_copy(update={"content": _encoded(self.session.last_edit)})
        return result

    def _ordered_batch(self, request: ToolCallRequest) -> tuple[_ToolBatch, int]:
        state = cast("Mapping[str, object]", request.state)
        messages = cast("list[BaseMessage]", state.get("messages", []))
        latest = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
        calls = latest.tool_calls if latest is not None else [request.tool_call]
        ids = tuple(str(call["id"]) for call in calls if call["name"] in self.allowed_tools)
        key = str(latest.id if latest is not None and latest.id else id(latest))
        with self._batch_lock:
            if self._batch is None or self._batch.key != key:
                self._batch = _ToolBatch(key, ids, self.session.request_revision)
            return self._batch, ids.index(str(request.tool_call["id"]))

    def _begin_tool(self, batch: _ToolBatch) -> None:
        if batch.failed:
            msg = "前序工具执行异常, 本批后续操作已停止"
            code = "batch_aborted"
            raise DraftError(code, msg, path=self.draft_path)
        self.session.request_revision = batch.revision

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """按模型给出的顺序执行同批工具, 各操作仍独立校验."""
        if request.tool_call["name"] not in self.allowed_tools:
            return self._execute(request, handler)
        batch, index = self._ordered_batch(request)
        if index:
            batch.sync[index - 1].wait()
        try:
            self._begin_tool(batch)
            return self._execute(request, handler)
        except BaseException:
            batch.failed = True
            raise
        finally:
            batch.revision = self.session.draft.revision
            batch.sync[index].set()

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """异步路径按同一顺序等待, 不阻塞事件循环."""
        if request.tool_call["name"] not in self.allowed_tools:
            return await self._aexecute(request, handler)
        batch, index = self._ordered_batch(request)
        if index:
            await batch.asynchronous[index - 1].wait()
        try:
            self._begin_tool(batch)
            return await self._aexecute(request, handler)
        except BaseException:
            batch.failed = True
            raise
        finally:
            batch.revision = self.session.draft.revision
            batch.asynchronous[index].set()

    def _execute(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """验证单个工具的会话状态和权限, 业务拒绝返回工具错误."""
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        try:
            return self._record_result(request, handler(request))
        except ValueError as error:
            self.session.record_rejection(validation=True)
            return ToolMessage(
                content=_encoded(error.as_dict() if isinstance(error, DraftError) else {"error": str(error)}),
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
                status="error",
            )

    async def _aexecute(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """异步路径使用同一权限检查和业务错误反馈."""
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        try:
            return self._record_result(request, await handler(request))
        except ValueError as error:
            self.session.record_rejection(validation=True)
            return ToolMessage(
                content=_encoded(error.as_dict() if isinstance(error, DraftError) else {"error": str(error)}),
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
                status="error",
            )
