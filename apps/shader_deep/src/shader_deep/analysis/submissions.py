"""保留未通过校验的完整提交, 通过局部修改复用原提交契约."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

from langchain.tools import tool
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from shader_deep.analysis.events import failure_signature
from shader_deep.analysis.schemas import AnalysisValidationError

if TYPE_CHECKING:
    from _thread import LockType
    from collections.abc import Callable
    from pathlib import Path

    from langchain.tools import BaseTool


class SubmissionChange(BaseModel):
    """只修改 JSON 数据, 不提供表达式、代码或复杂数组操作."""

    model_config = ConfigDict(extra="forbid")
    op: Literal["set", "remove"]
    path: str
    value: JsonValue = None


class RepairArguments(BaseModel):
    """局部修复入口的参数契约, 与工具校验共用."""

    model_config = ConfigDict(extra="forbid")
    draft_id: str
    expected_revision: int
    changes: list[SubmissionChange] = Field(min_length=1)


@dataclass(frozen=True)
class SubmissionReply:
    """提交工具回执及执行器使用的拒绝类别."""

    content: str
    category: str | None = None
    signature: str | None = None
    saved: dict[str, object] | None = field(default=None, kw_only=True)


def _segments(pointer: str) -> list[str]:
    """解析 JSON Pointer, 保留空字段并拒绝未定义的转义."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        msg = "JSON Pointer must be empty or start with /"
        raise ValueError(msg)
    parts = pointer[1:].split("/")
    for part in parts:
        escaped = part.replace("~1", "").replace("~0", "")
        if "~" in escaped:
            msg = f"Invalid JSON Pointer escape: {pointer}"
            raise ValueError(msg)
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _child(value: object, part: str) -> object:
    """仅访问对象字段或已有数组位置."""
    if isinstance(value, dict) and part in value:
        return value[part]
    if isinstance(value, list) and part.isascii() and part.isdigit() and int(part) < len(value):
        return value[int(part)]
    msg = f"JSON Pointer target does not exist: {part}"
    raise ValueError(msg)


def resolve_pointer(document: object, pointer: str) -> object:
    """读取对象中 JSON Pointer 指定的值.

    Args:
        document: 已解析的 JSON 数据.
        pointer: 空字符串表示整个文档, 其他路径以 / 开始.

    Returns:
        路径对应的原始值.

    Raises:
        ValueError: 路径格式错误或目标不存在.
    """
    value = document
    for part in _segments(pointer):
        value = _child(value, part)
    return value


def _apply_change(document: dict[str, object], change: SubmissionChange) -> dict[str, object]:
    """修改调用方提供的副本; 任一步失败都不会触及当前草稿."""
    parts = _segments(change.path)
    if change.op == "set" and "value" not in change.model_fields_set:
        msg = "set requires value; use an explicit null to set null"
        raise ValueError(msg)
    if not parts:
        if change.op != "set" or not isinstance(change.value, dict):
            msg = "Submission root can only be replaced with an object"
            raise ValueError(msg)
        return deepcopy(change.value)
    parent: object = document
    for part in parts[:-1]:
        parent = _child(parent, part)
    key = parts[-1]
    if change.op == "remove" or isinstance(parent, list):
        _child(parent, key)
    if isinstance(parent, dict):
        if change.op == "remove":
            del parent[key]
        else:
            parent[key] = deepcopy(change.value)
    elif isinstance(parent, list):
        if change.op == "remove":
            del parent[int(key)]
        else:
            parent[int(key)] = deepcopy(change.value)
    else:
        msg = "JSON Pointer parent must be an object or array"
        raise TypeError(msg)
    return document


def _schema_issues(exc: ValidationError) -> list[dict[str, object]]:
    """保留字段位置和原因, 不把大报告正文回显给模型."""
    issues: list[dict[str, object]] = []
    for item in exc.errors(include_input=False):
        path = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in item["loc"])
        nested = getattr(item.get("ctx", {}).get("error"), "issues", None)
        if isinstance(nested, list):
            issues.extend({**issue, "path": path.rstrip("/") + str(issue["path"])} for issue in nested)
        else:
            issues.append({"path": path, "code": item["type"], "message": f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"})
    return issues


def _apply_changes(document: dict[str, object], changes: list[SubmissionChange]) -> dict[str, object]:
    """定位无效补丁自身的参数, 同时保留完整目标 Pointer."""
    for index, change in enumerate(changes):
        try:
            document = _apply_change(document, change)
        except (TypeError, ValueError) as exc:
            invalid_value = change.op == "set" and (
                "value" not in change.model_fields_set or (change.path == "" and not isinstance(change.value, dict))
            )
            parameter = "value" if invalid_value else "path"
            issue: dict[str, object] = {"path": f"/changes/{index}/{parameter}", "code": "invalid_patch", "message": f"Target {change.path!r}: {exc}"}
            raise AnalysisValidationError([issue]) from exc
    return document


class SubmissionHandler:
    """一个任务共用一份草稿和串行提交边界, 成功后不再接受修改."""

    def __init__(
        self,
        task_id: str,
        submission_tool: BaseTool,
        submit: Callable[[dict[str, object]], str],
        finished: Callable[[], str | None],
        lock: LockType,
        *,
        directory: Path | None = None,
    ) -> None:
        """绑定原提交契约及不重复加锁的业务回调.

        Args:
            task_id: 当前主任务或子任务的固定标识.
            submission_tool: 保留原名称和参数结构的完整提交工具.
            submit: 接收校验后参数的业务提交函数, 不得再次获取 lock.
            finished: 已成功时返回原有结果回执, 否则返回 None.
            lock: 当前任务与其他工具共用的锁.
            directory: 本次运行目录; 提供时持久化独立草稿文件.
        """
        self.task_id, self.tool = task_id, submission_tool
        self.submit, self.finished, self.lock = submit, finished, lock
        self.directory = directory
        self.draft: dict[str, object] | None = None
        self._repair_tool = self._make_repair_tool()

    def _make_repair_tool(self) -> BaseTool:
        """工具函数保留直接调用路径, 循环内则在 schema 校验前统一路由."""

        @tool(args_schema=RepairArguments)
        def repair_analysis_submission(draft_id: str, expected_revision: int, changes: list[SubmissionChange]) -> str:
            """按 JSON Pointer 局部修复被拒绝的完整提交, 成功后自动重新校验和提交.

            Args:
                draft_id: 拒绝回执中的当前任务草稿标识.
                expected_revision: 拒绝回执中的草稿版本.
                changes: 依次执行 set 或 remove; 更名需 set 新字段并 remove 原字段, remove 的路径必须存在.
            """
            reply = self.handle(
                "repair_analysis_submission",
                {"draft_id": draft_id, "expected_revision": expected_revision, "changes": [item.model_dump(exclude_unset=True) for item in changes]},
            )
            return cast("SubmissionReply", reply).content

        return repair_analysis_submission

    def repair_tool(self) -> BaseTool:
        """返回本任务绑定的局部修复工具."""
        return self._repair_tool

    def snapshot(self) -> dict[str, object] | None:
        """返回短草稿索引, 完整内容仅保存到制品文件."""
        if self.draft is None:
            return None
        return {key: deepcopy(self.draft[key]) for key in ("draft_id", "revision", "tool_name")}

    def handle(self, name: str, arguments: dict[str, object]) -> SubmissionReply | None:
        """路由完整提交和修复; 无关工具返回 None.

        Args:
            name: 经执行白名单检查的工具名.
            arguments: 经严格 JSON 解析后的原始参数对象.

        Returns:
            原业务成功回执或可继续修复的错误; 不属于本处理器时返回 None.
        """
        if name not in {self.tool.name, self._repair_tool.name}:
            return None
        with self.lock:
            completed = self.finished()
            if completed is not None:
                return SubmissionReply(completed)
            previous = self.snapshot()
            if name == self._repair_tool.name:
                reply = self._repair(arguments)
            else:
                self._replace(arguments)
                reply = self._validate_and_submit()
            current = self.snapshot()
            # 保存归属在同一任务锁内确定, 并行的终态重复调用不能冒领本次保存事件.
            return replace(reply, saved=current if current != previous else None)

    def _replace(self, arguments: dict[str, object]) -> None:
        """完整提交或有效修改都推进版本, 旧版本不能覆盖新的内容."""
        revision = cast("int", self.draft["revision"]) + 1 if self.draft else 1
        self.draft = {
            "task_id": self.task_id,
            "draft_id": f"{self.task_id}-submission",
            "tool_name": self.tool.name,
            "revision": revision,
            "arguments": deepcopy(arguments),
            "errors": [],
        }
        self._save()

    def _repair(self, arguments: dict[str, object]) -> SubmissionReply:
        """校验版本并在副本上按序修改; 无效操作不改变任何草稿内容."""
        try:
            parsed = RepairArguments.model_validate(arguments)
            candidate = _apply_changes(self._editable_copy(parsed), parsed.changes)
        except ValidationError as exc:
            return self._rejection(_schema_issues(exc), "patch_rejection", save_errors=False)
        except AnalysisValidationError as exc:
            return self._rejection(exc.issues, "patch_rejection", save_errors=False)
        self._replace(candidate)
        return self._validate_and_submit()

    def _editable_copy(self, parsed: RepairArguments) -> dict[str, object]:
        """只允许修改当前任务的最新草稿."""
        if self.draft is None or parsed.draft_id != self.draft["draft_id"]:
            msg = "No matching current draft; use this task's draft_id"
            raise AnalysisValidationError([{"path": "/draft_id", "code": "invalid_patch", "message": msg}])
        if parsed.expected_revision != self.draft["revision"]:
            msg = "Draft revision changed; use its latest revision"
            raise AnalysisValidationError([{"path": "/expected_revision", "code": "invalid_patch", "message": msg}])
        return deepcopy(cast("dict[str, object]", self.draft["arguments"]))

    def _validate_and_submit(self) -> SubmissionReply:
        """只复用原 schema 与业务提交逻辑, 不把草稿登记为有效报告."""
        draft = cast("dict[str, object]", self.draft)
        try:
            validated = TypeAdapter(self.tool.get_input_schema()).validate_python(draft["arguments"])
            response = self.submit(dict(validated))
        except ValidationError as exc:
            return self._rejection(_schema_issues(exc), "schema_validation")
        except (KeyError, TypeError, ValueError) as exc:
            issues = getattr(exc, "issues", [{"path": "", "code": "business_rejection", "message": str(exc)}])
            return self._rejection(issues, "business_rejection")
        draft["errors"] = []
        draft["status"] = "submitted"
        self._save()
        return SubmissionReply(response)

    def _rejection(self, issues: list[dict[str, object]], category: str, *, save_errors: bool = True) -> SubmissionReply:
        """本地保留完整错误, 模型按每次返回的少量问题继续局部修复."""
        if self.draft is not None and save_errors:
            self.draft["errors"] = issues
            self._save()
        payload = {
            "status": "invalid_submission",
            **(self.snapshot() or {}),
            "errors": issues[:4],
            "remaining_errors": max(0, len(issues) - 4),
            "message": (
                "Listed errors are those detected so far; remaining_errors counts only omitted detected errors. "
                + {
                    "schema_validation": "Submission schema is invalid; later submission business checks have not run. Repair and validate again.",
                    "patch_rejection": "The patch was not applied; the draft is unchanged and submission checks have not rerun.",
                }.get(category, "Business validation stopped at a failed prerequisite; later checks may still report errors after repair.")
            ),
            "repair_tool": self._repair_tool.name,
        }
        arguments = self.draft["arguments"] if self.draft else None
        return SubmissionReply(json.dumps(payload, ensure_ascii=False), category, failure_signature(self.tool.name, arguments, issues))

    def _save(self) -> None:
        """按任务保存完整参数与错误, 原子替换单个文件."""
        if self.directory is None or self.draft is None:
            return
        destination = self.directory / "submissions" / f"{self.task_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.draft, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
