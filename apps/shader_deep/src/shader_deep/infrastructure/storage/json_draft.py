"""单份 JSON 草稿的磁盘来源、版本核对及局部原子修改."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Generic, NoReturn, TypeVar, cast

from pydantic import BaseModel, JsonValue, ValidationError

if TYPE_CHECKING:
    from _thread import RLock as RLockType
    from collections.abc import Callable

DRAFT_PATH = "/draft/observations.json"
DraftModel = TypeVar("DraftModel", bound=BaseModel)


class DraftError(ValueError):
    """可修复的草稿操作拒绝, 携带稳定错误码."""

    def __init__(self, code: str, message: str, *, path: str = DRAFT_PATH) -> None:
        """保存操作错误的定位信息."""
        super().__init__(message)
        self.code, self.path = code, path

    def as_dict(self) -> dict[str, JsonValue]:
        """返回可直接用于工具反馈的错误."""
        return {"code": self.code, "path": self.path, "message": str(self)}


def _reject(code: str, message: str) -> NoReturn:
    raise DraftError(code, message)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _object_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            msg = f"JSON 对象键 {key!r} 重复"
            _reject("invalid_json", msg)
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    msg = f"JSON 不允许 {value}"
    _reject("invalid_json", msg)


def _changes(before: JsonValue, after: JsonValue, path: str = "") -> list[dict[str, JsonValue]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict[str, JsonValue]] = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key in before and key in after:
                result.extend(_changes(before[key], after[key], child))
            else:
                result.append({"operation": "remove" if key in before else "add", "path": child, "before": before.get(key), "after": after.get(key)})
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for offset in range(max(len(before), len(after))):
            child = f"{path}/{offset}"
            if offset < min(len(before), len(after)):
                result.extend(_changes(before[offset], after[offset], child))
            else:
                result.append(
                    {
                        "operation": "remove" if offset < len(before) else "add",
                        "path": child,
                        "before": before[offset] if offset < len(before) else None,
                        "after": after[offset] if offset < len(after) else None,
                    }
                )
        return result
    return [{"operation": "replace", "path": path, "before": before, "after": after}]


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    committed = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        Path(temporary).replace(path)
        committed = True
    finally:
        if not committed:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


class JsonDraftStore(Generic[DraftModel]):
    """共享操作锁下的一份草稿, revision 始终由实际提交字节决定."""

    def __init__(
        self,
        run_dir: Path,
        *,
        filename: str,
        model_type: type[DraftModel],
        validation: Callable[[DraftModel], dict[str, JsonValue]],
        lock: RLockType | None = None,
    ) -> None:
        """固定运行目录, 不接管已有草稿或外部文件.

        Args:
            run_dir: 本次独立运行的目录.
            filename: 固定的草稿文件名.
            model_type: 只约束字段结构的提交类型.
            validation: 返回跨条目业务诊断的回调.
            lock: 调用方共用的可重入操作锁.
        """
        self.lock = lock if lock is not None else RLock()
        self.directory = run_dir / "draft"
        self.path = self.directory / filename
        self.virtual_path = f"/draft/{filename}"
        self.model_type = model_type
        self.validation = validation
        self.history = self.directory / "history"
        self.revision: str | None = None

    def _reject(self, code: str, message: str) -> NoReturn:
        raise DraftError(code, message, path=self.virtual_path)

    def _parse(self, content: str) -> DraftModel:
        try:
            value = json.loads(content, object_pairs_hook=_object_pairs, parse_constant=_nonfinite)
        except DraftError as error:
            raise DraftError(error.code, str(error), path=self.virtual_path) from error
        except json.JSONDecodeError as error:
            self._reject("invalid_json", f"JSON 第 {error.lineno} 行第 {error.colno} 列无法解析")
        try:
            return self.model_type.model_validate(value)
        except ValidationError as error:
            self._reject("invalid_fields", str(error))

    def _safe_paths(self) -> None:
        if self.directory.is_symlink() or self.path.is_symlink() or self.history.is_symlink():
            msg = "草稿路径不可为符号链接"
            self._reject("invalid_path", msg)

    def read(self, *, expected_revision: str | None = None) -> tuple[str, str]:
        """读取当前草稿并核对外部变更及可选预期版本.

        Args:
            expected_revision: 调用所基于的已提交版本.

        Returns:
            原始文本与实际版本.
        """
        with self.lock:
            self._safe_paths()
            if self.revision is None:
                msg = "当前运行尚未创建草稿"
                self._reject("draft_missing", msg)
            content = self.path.read_bytes()
            actual = _digest(content)
            if actual != self.revision:
                msg = "草稿已被工具以外的写入修改"
                self._reject("external_draft_change", msg)
            if expected_revision is not None and actual != expected_revision:
                msg = "调用依据的草稿版本已过期, 请回读"
                self._reject("stale_draft", msg)
            return content.decode("utf-8"), actual

    def report(self, *, expected_revision: str | None = None) -> DraftModel:
        """读取同一草稿的字段结构对象.

        Args:
            expected_revision: 可选预期版本.

        Returns:
            字段合法但可能仍有引用问题的草稿.
        """
        return self._parse(self.read(expected_revision=expected_revision)[0])

    def create(self, report: DraftModel) -> dict[str, JsonValue]:
        """首次保存字段合法的完整草稿, 已有文件保持不变.

        Args:
            report: 草稿, 可以包含跨条目业务错误.

        Returns:
            创建状态、版本、路径与业务诊断.
        """
        with self.lock:
            self._safe_paths()
            if self.revision is not None:
                current = self.report()
                return {"created": False, "path": self.virtual_path, "revision": self.revision, "validation": self.validation(current)}
            if self.path.exists():
                msg = "已有草稿不属于当前运行实例"
                self._reject("external_draft_change", msg)
            current = self.model_type.model_validate(report.model_dump(mode="json", exclude_none=True))
            content = json.dumps(current.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2).encode("utf-8")
            self.directory.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, content)
            self.revision = _digest(content)
            return {"created": True, "saved": True, "path": self.virtual_path, "revision": self.revision, "validation": self.validation(current)}

    def _snapshot(self, content: bytes, revision: str) -> None:
        self.history.mkdir(exist_ok=True)
        target = self.history / f"{revision}.json"
        if target.is_symlink():
            msg = "历史快照不可为符号链接"
            self._reject("invalid_path", msg)
        if target.exists():
            if target.read_bytes() != content:
                msg = "历史快照内容与版本不一致"
                self._reject("history_conflict", msg)
        else:
            _atomic_write(target, content)

    def edit(self, old_string: str, new_string: str, *, expected_revision: str | None, replace_all: bool = False) -> dict[str, JsonValue]:
        """以唯一匹配局部修改草稿, 成功提交后保留旧字节快照.

        Args:
            old_string: 必须唯一匹配的局部文本.
            new_string: 替换后的局部文本.
            expected_revision: 产生本次修改的模型请求版本.
            replace_all: 仅允许 false.

        Returns:
            保存状态、前后版本、修改摘要与业务诊断.
        """
        with self.lock:
            content, revision = self.read()
            if expected_revision != revision:
                msg = "调用依据的草稿版本已过期, 请回读"
                self._reject("stale_draft", msg)
            self._check_replacement(content, old_string, replace_all=replace_all)
            changed = content.replace(old_string, new_string, 1)
            report = self._parse(changed)
            changes = _changes(cast("JsonValue", json.loads(content)), cast("JsonValue", json.loads(changed)))
            if changed != content:
                self._snapshot(content.encode("utf-8"), revision)
                _atomic_write(self.path, changed.encode("utf-8"))
                self.revision = _digest(changed.encode("utf-8"))
            return {
                "saved": True,
                "path": self.virtual_path,
                "before_revision": revision,
                "revision": self.revision,
                "changes": changes,
                "validation": self.validation(report),
            }

    def _check_replacement(self, content: str, old_string: str, *, replace_all: bool) -> None:
        if replace_all or not old_string or content.count(old_string) != 1:
            msg = "只允许非空且唯一匹配的局部替换, replace_all 必须为 false"
            self._reject("non_unique_replacement", msg)
        if old_string.strip() == content.strip():
            msg = "请修改局部片段, 不可替换完整草稿"
            self._reject("whole_document_replace", msg)
