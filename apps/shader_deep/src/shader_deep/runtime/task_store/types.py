"""持久任务运行的值对象与严格 JSON 边界."""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class Attempt:
    """只有当前代次的活跃实例具有新提交权限."""

    task_id: str
    attempt_id: str
    generation: int
    input_version: str


@dataclass(frozen=True)
class Receipt:
    """原子提交后的稳定回执, 通知丢失不影响查询."""

    submission_id: str
    task_id: str
    result_path: str
    fingerprint: str


def _validate_json(value: object, parents: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if not isinstance(value, (list, dict)) or id(value) in parents:
        msg = "值必须是有限、无循环的 JSON 数据"
        raise ValueError(msg)
    parents.add(id(value))
    try:
        _validate_container(value, parents)
    finally:
        parents.remove(id(value))


def _validate_container(value: list[object] | dict[object, object], parents: set[int]) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            msg = "JSON 对象的键必须为字符串"
            raise ValueError(msg)
        values = value.values()
    else:
        values = value
    for item in values:
        _validate_json(item, parents)


def encode(value: JsonValue) -> bytes:
    """校验并编码规范 JSON, 保留有限数值和明确键类型.

    Args:
        value: 待保存的 JSON 内容.

    Returns:
        用于持久保存和内容指纹的 UTF-8 字节.

    Raises:
        ValueError: 包含非 JSON 类型、非有限值或循环.
    """
    _validate_json(value, set())
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(data: bytes) -> str:
    """计算规范内容指纹."""
    return hashlib.sha256(data).hexdigest()


def copy_json(value: JsonValue) -> JsonValue:
    """返回经校验的深复制, 调用方不能修改运行内存."""
    return cast("JsonValue", json.loads(encode(value)))


def object_value(value: JsonValue) -> JsonObject:
    """读取持久记录中的对象, 拒绝错误容器类型."""
    if not isinstance(value, dict):
        msg = "任务运行记录必须包含 JSON 对象"
        raise TypeError(msg)
    return value


def string_value(value: JsonValue) -> str:
    """读取持久记录中的字符串."""
    if not isinstance(value, str):
        msg = "任务运行记录的字符串字段不合法"
        raise TypeError(msg)
    return value


def integer_value(value: JsonValue) -> int:
    """读取持久记录中的整数计数."""
    if not isinstance(value, int) or isinstance(value, bool):
        msg = "任务运行记录的计数字段不合法"
        raise TypeError(msg)
    return value
