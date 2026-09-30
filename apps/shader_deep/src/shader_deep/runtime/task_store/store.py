"""任务状态、回执与版本指针通过一个提交文件一起发布."""

import fcntl
import threading
import uuid
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from shader_deep.runtime.task_store import files
from shader_deep.runtime.task_store.types import (
    Attempt,
    JsonObject,
    JsonValue,
    Receipt,
    copy_json,
    encode,
    fingerprint,
    integer_value,
    object_value,
    string_value,
)

MAX_ATTEMPTS = 3
MAX_REPAIRS = 2


def _table(state: JsonObject, name: str) -> JsonObject:
    return object_value(state[name])


def _task(state: JsonObject, task_id: str) -> JsonObject:
    return object_value(_table(state, "tasks")[task_id])


def _attempts(task: JsonObject) -> list[JsonValue]:
    value = task["attempts"]
    if not isinstance(value, list):
        msg = "attempts 必须是列表"
        raise TypeError(msg)
    return value


def _attempt_record(task: JsonObject, attempt_id: str) -> JsonObject:
    for value in _attempts(task):
        record = object_value(value)
        if record["attempt_id"] == attempt_id:
            return record
    msg = "执行实例不存在"
    raise ValueError(msg)


def _receipt(value: JsonValue) -> Receipt:
    record = object_value(value)
    return Receipt(
        submission_id=string_value(record["submission_id"]),
        task_id=string_value(record["task_id"]),
        result_path=string_value(record["result_path"]),
        fingerprint=string_value(record["fingerprint"]),
    )


def _initial_state() -> JsonObject:
    return {
        "schema_version": "task_store_v1",
        "generation": 0,
        "revision": 0,
        "sealed": False,
        "package_path": None,
        "tasks": {},
        "receipts": {},
        "versions": {},
    }


class TaskStore:
    """每轮单协调器持有进程锁, 所有发布共用原子提交记录.

    Args:
        directory: 独立运行目录, 包含 commit.json 与不可变结果.
    """

    def __init__(self, directory: Path) -> None:
        """创建尚未获取进程所有权的存储句柄."""
        self.directory = directory
        self._mutex = threading.RLock()
        self._owner: BinaryIO | None = None
        self._state: JsonObject | None = None

    def __enter__(self) -> Self:
        """取得独占权并从提交记录恢复, 撤销旧代次执行权."""
        with self._mutex:
            if self._owner is not None:
                msg = "TaskStore 已经打开"
                raise RuntimeError(msg)
            self.directory.mkdir(parents=True, exist_ok=True)
            self._owner = (self.directory / "coordinator.lock").open("a+b")
            try:
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._load_and_recover()
            except BaseException:
                self._release()
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """释放进程锁, 后续协调器根据持久记录接管."""
        with self._mutex:
            self._release()

    def _release(self) -> None:
        if self._owner is not None:
            self._owner.close()
        self._owner = None
        self._state = None

    def _load_and_recover(self) -> None:
        path = self.directory / "commit.json"
        state = object_value(files.read_json(path)) if path.exists() else _initial_state()
        if state["schema_version"] != "task_store_v1":
            msg = "不支持的任务存储格式"
            raise ValueError(msg)
        self._state = state
        recovered = self._copy_state()
        recovered["generation"] = integer_value(state["generation"]) + 1
        for value in _table(recovered, "tasks").values():
            task = object_value(value)
            task.setdefault("model_calls", 0)
            self._recover_task(task)
        self._publish(recovered)

    @staticmethod
    def _recover_task(task: JsonObject) -> None:
        if task["status"] != "running":
            return
        for value in _attempts(task):
            record = object_value(value)
            if record["status"] == "running":
                record["status"] = "interrupted"
                record["error"] = "协调器中断, 执行实例失效"
        task["status"] = "pending" if len(_attempts(task)) < MAX_ATTEMPTS else "failed"
        task["error"] = "协调器中断, 执行实例失效"

    def _current(self) -> JsonObject:
        if self._owner is None or self._state is None:
            msg = "TaskStore 必须在 context manager 中使用"
            raise RuntimeError(msg)
        return self._state

    def _copy_state(self) -> JsonObject:
        return object_value(copy_json(self._current()))

    def _publish(self, state: JsonObject) -> None:
        state["revision"] = integer_value(self._current()["revision"]) + 1
        files.atomic_write(self.directory / "commit.json", encode(state))
        # 磁盘提交成功才更新可见内存, IO 失败保留上一版本.
        self._state = state

    def _require_open(self) -> None:
        if self._current()["sealed"]:
            msg = "运行已经封存"
            raise ValueError(msg)

    def register(self, task_id: str, input_version: str, payload: JsonValue) -> None:
        """登记固定输入的逻辑任务, 相同输入重复登记幂等.

        Args:
            task_id: 本轮逻辑任务身份.
            input_version: 固定输入版本.
            payload: 完整 JSON 任务材料.

        Raises:
            ValueError: 同一身份改写输入、非法 JSON 或运行封存.
        """
        data = encode(payload)
        with self._mutex:
            if self._registered(task_id, input_version, fingerprint(data)):
                return
            self._require_open()
            self._register(task_id, input_version, payload, fingerprint(data))

    def _registered(self, task_id: str, version: str, digest: str) -> bool:
        tasks = _table(self._current(), "tasks")
        if task_id not in tasks:
            if not task_id or not version:
                msg = "任务 ID 和输入版本不能为空"
                raise ValueError(msg)
            return False
        record = object_value(tasks[task_id])
        if record["input_version"] != version or record["input_fingerprint"] != digest:
            msg = "已登记任务的输入不可改变"
            raise ValueError(msg)
        return True

    def _register(self, task_id: str, version: str, payload: JsonValue, digest: str) -> None:
        state = self._copy_state()
        _table(state, "tasks")[task_id] = {
            "input_version": version,
            "input_fingerprint": digest,
            "payload": copy_json(payload),
            "status": "pending",
            "attempts": [],
            "repairs": 0,
            "model_calls": 0,
            "result_path": None,
            "error": None,
        }
        self._publish(state)

    def start_attempt(self, task_id: str) -> Attempt:
        """启动下一实例, 同一逻辑任务最多初始加两次重派.

        Args:
            task_id: 已登记的逻辑任务.

        Returns:
            绑定当前运行代次和固定输入版本的实例身份.

        Raises:
            ValueError: 不在 pending、实例额度耗尽或运行封存.
        """
        with self._mutex:
            self._require_open()
            state = self._copy_state()
            task = _task(state, task_id)
            if task["status"] != "pending" or len(_attempts(task)) >= MAX_ATTEMPTS:
                msg = "任务不接受新实例或重派次数已耗尽"
                raise ValueError(msg)
            attempt = self._start(state, task, task_id)
            self._publish(state)
            return attempt

    @staticmethod
    def _start(state: JsonObject, task: JsonObject, task_id: str) -> Attempt:
        attempt = Attempt(
            task_id,
            uuid.uuid4().hex,
            integer_value(state["generation"]),
            string_value(task["input_version"]),
        )
        _attempts(task).append(
            {
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "input_version": attempt.input_version,
                "status": "running",
                "error": None,
            }
        )
        task["status"] = "running"
        task["error"] = None
        return attempt

    def is_active(self, attempt: Attempt) -> bool:
        """核对程序持久状态, 不依赖模型回复或完成事件."""
        with self._mutex:
            state = self._current()
            if state["sealed"] or state["generation"] != attempt.generation:
                return False
            return self._matches(state, attempt)

    @staticmethod
    def _matches(state: JsonObject, attempt: Attempt) -> bool:
        tasks = _table(state, "tasks")
        if attempt.task_id not in tasks:
            return False
        task = object_value(tasks[attempt.task_id])
        if task["status"] != "running" or task["input_version"] != attempt.input_version:
            return False
        for value in _attempts(task):
            record = object_value(value)
            if record["attempt_id"] == attempt.attempt_id:
                return record["status"] == "running" and record["generation"] == attempt.generation
        return False

    def _require_active(self, attempt: Attempt) -> None:
        if not self.is_active(attempt):
            msg = "任务终结、实例失效或运行封存, 拒绝新写入"
            raise ValueError(msg)

    def _require_parent_pending(self, parent_task_id: str | None) -> None:
        if parent_task_id is None:
            return
        tasks = _table(self._current(), "tasks")
        if parent_task_id not in tasks or object_value(tasks[parent_task_id])["status"] != "pending":
            msg = "原任务不再等待恢复, 拒绝恢复实例的新写入"
            raise ValueError(msg)

    def consume_repair(self, attempt: Attempt) -> None:
        """消耗逻辑任务共享的修复额度, 重派后不重置."""
        with self._mutex:
            self._require_active(attempt)
            state = self._copy_state()
            task = _task(state, attempt.task_id)
            repairs = integer_value(task["repairs"])
            if repairs >= MAX_REPAIRS:
                msg = "逻辑任务的两次修复额度已耗尽"
                raise ValueError(msg)
            task["repairs"] = repairs + 1
            self._publish(state)

    def consume_model_call(self, attempt: Attempt, limit: int = 0, *, parent_task_id: str | None = None) -> int:
        """发送前持久扣除一次逻辑调用, 重派和协调器恢复不重置.

        Args:
            attempt: 具有当前写权的执行实例.
            limit: 本逻辑任务的显式累计上限, 0 表示不限制.
            parent_task_id: 恢复角色绑定的原任务, 新扣费时必须仍 pending.

        Returns:
            扣除后的逻辑调用累计次数.

        Raises:
            ValueError: 上限不合法、累计额度耗尽或实例失效.
            OSError: 提交失败, 已接受计数保持不变.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            msg = "逻辑调用上限必须是非负整数"
            raise ValueError(msg)
        with self._mutex:
            self._require_active(attempt)
            self._require_parent_pending(parent_task_id)
            state = self._copy_state()
            task = _task(state, attempt.task_id)
            calls = integer_value(task["model_calls"])
            if limit and calls >= limit:
                msg = "逻辑模型调用额度已耗尽"
                raise ValueError(msg)
            task["model_calls"] = calls + 1
            self._publish(state)
            return calls + 1

    def submit(self, attempt: Attempt, submission_id: str, payload: JsonValue, *, parent_task_id: str | None = None) -> Receipt:
        """先查既有回执, 再原子核对写权并发布结果、终态与回执.

        Args:
            attempt: 当前实例身份, 重复成功提交可来自旧实例.
            submission_id: 本轮唯一提交身份.
            payload: 完整且经上层业务校验的结果.
            parent_task_id: 恢复角色绑定的原任务, 新提交时必须仍 pending.

        Returns:
            新提交或原成功提交的稳定回执.

        Raises:
            ValueError: 身份对应异内容、实例失效或内容非法.
            OSError: 持久写入失败, 已接受状态保持不变.
        """
        data = encode(payload)
        with self._mutex:
            existing = self._existing_receipt(attempt, submission_id, fingerprint(data))
            if existing is not None:
                return existing
            self._validate_submission(attempt, submission_id, fingerprint(data), parent_task_id)
            path = files.immutable_file(self.directory, data)
            return self._accept(attempt, submission_id, path, fingerprint(data))

    def _existing_receipt(self, attempt: Attempt, submission_id: str, digest: str) -> Receipt | None:
        receipts = _table(self._current(), "receipts")
        if submission_id not in receipts:
            return None
        receipt = _receipt(receipts[submission_id])
        if receipt.task_id != attempt.task_id or receipt.fingerprint != digest:
            msg = "同一提交身份不能绑定不同任务或内容"
            raise ValueError(msg)
        return receipt

    def _validate_submission(self, attempt: Attempt, submission_id: str, digest: str, parent_task_id: str | None) -> None:
        if not submission_id:
            msg = "提交身份不能为空"
            raise ValueError(msg)
        try:
            self._require_active(attempt)
            self._require_parent_pending(parent_task_id)
        except ValueError as error:
            self._audit(attempt, submission_id, digest, str(error))
            raise

    def _audit(self, attempt: Attempt, submission_id: str, digest: str, reason: str) -> None:
        record: JsonObject = {
            "task_id": attempt.task_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
            "submission_id": submission_id,
            "fingerprint": digest,
            "reason": reason,
        }
        try:
            with (self.directory / "late-submissions.jsonl").open("ab") as stream:
                stream.write(encode(record) + b"\n")
        except OSError:
            # 审计不拥有提交权, 其故障不改变已接受记录或遮蔽拒绝原因.
            return

    def _accept(self, attempt: Attempt, identity: str, path: str, digest: str) -> Receipt:
        state = self._copy_state()
        task = _task(state, attempt.task_id)
        _attempt_record(task, attempt.attempt_id)["status"] = "succeeded"
        task["status"] = "succeeded"
        task["result_path"] = path
        task["error"] = None
        _table(state, "receipts")[identity] = {
            "submission_id": identity,
            "task_id": attempt.task_id,
            "result_path": path,
            "fingerprint": digest,
        }
        self._publish(state)
        return _receipt(_table(state, "receipts")[identity])

    def fail_attempt(self, attempt: Attempt, error: str, *, permanent: bool = False) -> None:
        """结束活跃实例, 永久错误或第三实例失败才关闭逻辑任务."""
        with self._mutex:
            self._require_active(attempt)
            state = self._copy_state()
            task = _task(state, attempt.task_id)
            record = _attempt_record(task, attempt.attempt_id)
            record["status"] = "failed"
            record["error"] = error
            task["status"] = "failed" if permanent or len(_attempts(task)) >= MAX_ATTEMPTS else "pending"
            task["error"] = error
            self._publish(state)

    def cancel(self, task_id: str, reason: str) -> None:
        """取消与提交共用锁, 已成功或已失败终态不会回退."""
        with self._mutex:
            state = self._copy_state()
            task = _task(state, task_id)
            if task["status"] in ("succeeded", "failed", "cancelled"):
                return
            self._require_open()
            self._cancel(task, reason)
            self._publish(state)

    @staticmethod
    def _cancel(task: JsonObject, reason: str) -> None:
        task["status"] = "cancelled"
        task["error"] = reason
        for value in _attempts(task):
            record = object_value(value)
            if record["status"] == "running":
                record["status"] = "cancelled"
                record["error"] = reason

    def task(self, task_id: str) -> JsonObject:
        """返回任务的独立深复制, 可直接补查完成情况."""
        with self._mutex:
            return object_value(copy_json(_task(self._current(), task_id)))

    def read_result(self, task_id: str) -> JsonValue:
        """从唯一已提交结果指针读取, 暂存文件不可冒充结果."""
        with self._mutex:
            task = _task(self._current(), task_id)
            if task["status"] != "succeeded":
                msg = "任务没有成功提交的结果"
                raise ValueError(msg)
            receipt = self._result_receipt(task_id)
            if task["result_path"] != receipt.result_path:
                msg = "任务结果指针与成功提交回执不一致"
                raise ValueError(msg)
            return files.read_immutable_json(self.directory, receipt.result_path, receipt.fingerprint)

    def _result_receipt(self, task_id: str) -> Receipt:
        for value in _table(self._current(), "receipts").values():
            receipt = _receipt(value)
            if receipt.task_id == task_id:
                return receipt
        msg = "成功任务缺少原子提交回执"
        raise ValueError(msg)

    def snapshot(self) -> JsonObject:
        """从唯一提交记录派生运行快照, 不暴露可写内部状态."""
        with self._mutex:
            return self._copy_state()

    def publish_version(self, version: str, payload: JsonValue) -> str:
        """发布不可变版本指针, 相同版本同内容重复调用幂等."""
        data = encode(payload)
        with self._mutex:
            versions = _table(self._current(), "versions")
            if version in versions:
                return self._existing_version(versions[version], fingerprint(data))
            self._require_open()
            if not version:
                msg = "版本名称不能为空"
                raise ValueError(msg)
            return self._publish_version(version, data)

    @staticmethod
    def _existing_version(value: JsonValue, digest: str) -> str:
        version = object_value(value)
        if version["fingerprint"] != digest:
            msg = "已发布版本不可覆盖"
            raise ValueError(msg)
        return string_value(version["path"])

    def _publish_version(self, version: str, data: bytes) -> str:
        path = files.immutable_file(self.directory, data)
        state = self._copy_state()
        _table(state, "versions")[version] = {"path": path, "fingerprint": fingerprint(data)}
        self._publish(state)
        return path

    def read_version(self, version: str) -> JsonValue:
        """读取已发布版本, 半成品与未提交路径不可见."""
        with self._mutex:
            record = object_value(_table(self._current(), "versions")[version])
            return files.read_immutable_json(self.directory, string_value(record["path"]), string_value(record["fingerprint"]))

    def seal(self, *, package_path: str | None = None) -> None:
        """原子记录交付包路径并撤销全部新提交权."""
        with self._mutex:
            if self._current()["sealed"]:
                if self._current()["package_path"] != package_path:
                    msg = "已封存的交付包路径不可改变"
                    raise ValueError(msg)
                return
            state = self._copy_state()
            state["sealed"] = True
            state["package_path"] = package_path
            for value in _table(state, "tasks").values():
                task = object_value(value)
                if task["status"] in ("pending", "running"):
                    self._cancel(task, "运行已封存")
            self._publish(state)
