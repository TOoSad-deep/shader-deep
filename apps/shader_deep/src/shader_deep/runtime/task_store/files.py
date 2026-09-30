"""同文件系统的不可变数据文件和唯一提交记录发布."""

import json
import os
import tempfile
from pathlib import Path
from typing import cast

from shader_deep.runtime.task_store.types import JsonValue, encode, fingerprint


def read_json(path: Path) -> JsonValue:
    """读取并重新校验磁盘 JSON 内容."""
    value = cast("JsonValue", json.loads(path.read_bytes()))
    encode(value)
    return value


def atomic_write(path: Path, data: bytes) -> None:
    """先同步临时文件, 再原子替换作为发布点."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def immutable_file(directory: Path, data: bytes) -> str:
    """保存不可变内容, 未获提交指针引用时只是暂存数据."""
    relative = Path("results") / f"{fingerprint(data)}.json"
    path = directory / relative
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            msg = "不可变结果文件与内容指纹不一致"
            raise ValueError(msg)
    else:
        atomic_write(path, data)
    if encode(read_json(path)) != data:
        msg = "不可变结果落盘校验失败"
        raise ValueError(msg)
    return relative.as_posix()


def read_immutable_json(directory: Path, relative: str, digest: str) -> JsonValue:
    """按提交指纹读取不可变正文, 拒绝语法合法但内容被修改的文件."""
    if relative != f"results/{digest}.json":
        msg = "不可变结果路径与提交内容指纹不一致"
        raise ValueError(msg)
    value = read_json(directory / relative)
    if fingerprint(encode(value)) != digest:
        msg = "不可变结果内容与提交指纹不一致"
        raise ValueError(msg)
    return value
