"""环境外部配置的安全读取, 不决定业务默认值或覆盖优先级."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path


def read_yaml_mapping(path: Path, *, label: str) -> dict[object, object]:
    """加载 YAML 对象; 空文件返回空映射.

    Args:
        path: 调用方选择的配置路径.
        label: 错误消息中的业务配置名称.
    """
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"Invalid {label} YAML: {path}: {exc}"
        raise ValueError(msg) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        msg = f"{label.capitalize()} YAML must contain a parameter mapping: {path}"
        raise TypeError(msg)
    return payload
