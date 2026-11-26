"""按 VISTA 公开磁盘协议发布图片, 为工作流保存可独立校验的不可变 JSON."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import mkstemp

from pydantic import BaseModel, ConfigDict, JsonValue


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _directory(root: Path, name: str) -> Path:
    directory = root / name
    if root.is_symlink() or directory.is_symlink():
        msg = "Archive directories cannot be symbolic links"
        raise ValueError(msg)
    return directory


def _file(directory: Path, name: str) -> Path:
    if not name or Path(name).name != name or (directory / name).is_symlink():
        msg = f"Invalid archive filename: {name!r}"
        raise ValueError(msg)
    return directory / name


def _publish(directory: Path, suffix: str, content: bytes) -> Path:
    destination = _file(directory, _digest(content) + suffix)
    if not destination.exists():
        directory.mkdir(parents=True, exist_ok=True)
        _publish_file(directory, destination, content)
    if _file(directory, destination.name).read_bytes() != content:
        msg = f"Immutable archive content has changed: {destination.name}"
        raise ValueError(msg)
    return destination


def _install_file(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError:
        return
    except OSError as error:
        unsupported = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}
        if error.errno not in unsupported and getattr(error, "winerror", None) not in {1, 50}:
            raise
        # 同一地址的并行发布提交相同字节, 可在硬链不可用时原子安装写齐的文件.
        if not _file(destination.parent, destination.name).exists():
            temporary.replace(destination)


def _publish_file(directory: Path, destination: Path, content: bytes) -> None:
    descriptor, name = mkstemp(dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        _install_file(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read(directory: Path, name: str, suffix: str) -> bytes:
    if re.fullmatch(r"[0-9a-f]{64}" + re.escape(suffix), name) is None:
        msg = f"Invalid content-addressed archive entry: {name}"
        raise ValueError(msg)
    path = _file(directory, name)
    content = path.read_bytes()
    if name != _digest(content) + suffix:
        msg = f"Archive digest mismatch: {name}"
        raise ValueError(msg)
    return content


class _ImageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    png: str
    glsl: str | None
    source_agent: str | None
    render: dict[str, JsonValue] | None = None


def publish_image(
    root: Path, *, label: str, png: bytes, code: bytes | None = None, source_agent: str | None = None, render: dict[str, JsonValue] | None = None
) -> str:
    """将已校验的图片、可选 GLSL 和渲染参数发布为标准 VISTA 记录.

    Args:
        root: 本运行的 VISTA 图库根目录.
        label: 业务给出的记录名.
        png: 原始无损 PNG.
        code: 生成此图的 UTF-8 GLSL 字节.
        source_agent: 产生图片的角色.
        render: 此帧实际使用的外部参数.

    Returns:
        可交给 VistaStore 或 MCP read_record 的记录文件名.
    """
    image = _publish(_directory(root, "png"), ".png", png)
    shader = _publish(_directory(root, "glsl"), ".glsl", code) if code is not None else None
    data = _ImageRecord(
        label=label, png=f"png/{image.name}", glsl=f"glsl/{shader.name}" if shader else None, source_agent=source_agent, render=render
    )
    fields = data.model_dump(mode="json", exclude={"render"} if render is None else set())
    return _publish(_directory(root, "json"), ".json", _json(fields)).name


def read_image(root: Path, record: str) -> tuple[bytes, bytes | None, dict[str, JsonValue] | None]:
    """按记录名回读原 PNG、可选代码和渲染参数, 逐个核对内容身份."""
    data = _ImageRecord.model_validate_json(_read(_directory(root, "json"), record, ".json"))
    image = _read(_directory(root, "png"), data.png.removeprefix("png/"), ".png")
    code = _read(_directory(root, "glsl"), data.glsl.removeprefix("glsl/"), ".glsl") if data.glsl is not None else None
    return image, code, data.render


def list_images(root: Path) -> list[dict[str, JsonValue]]:
    """列出本工作流图库中的记录名和来源, 目录读取不加载图片."""
    directory = _directory(root, "json")
    results: list[dict[str, JsonValue]] = []
    for path in sorted(directory.glob("*.json")):
        data = _ImageRecord.model_validate_json(_read(directory, path.name, ".json"))
        results.append({"record": path.name, "label": data.label, "source_agent": data.source_agent, "has_glsl": data.glsl is not None})
    return results


def publish_history_json(root: Path, value: object) -> str:
    """在业务侧保留评审、测量、参数或最佳选择的不可变版本."""
    return _publish(_directory(root, "history"), ".json", _json(value)).name


def read_history_json(root: Path, record: str) -> dict[str, JsonValue]:
    """核对业务清单的内容身份并恢复该版本."""
    data = json.loads(_read(_directory(root, "history"), record, ".json"))
    if not isinstance(data, dict):
        msg = "Workflow history record must be an object"
        raise TypeError(msg)
    return data
