"""保存一次生成运行的代码、图像和业务记录."""

from __future__ import annotations

import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.infrastructure.storage.snapshots import write_snapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shader_deep.domain.tasks import BlackboardState


def create_run_directory(parent: Path | None = None) -> Path:
    """创建独立运行目录, 保留调用方已有文件.

    Args:
        parent: 运行目录的父目录, 未提供时使用当前目录下的 runs.

    Returns:
        本次运行的唯一绝对路径.
    """
    root = (parent if parent is not None else Path("runs")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # mkdtemp 生成唯一目录名, 避免两次运行的 candidate-001.glsl 相互覆盖.
    # 这里不使用 TemporaryDirectory: 运行结束后需要保留制品给用户查看.
    return Path(tempfile.mkdtemp(prefix="run-", dir=root))


def save_run(directory: Path, state: BlackboardState, details: Mapping[str, object]) -> None:
    """保存可定位候选和失败原因的运行快照.

    Args:
        directory: 本次运行目录.
        state: 当前业务黑板, 不包含模型连接配置或内部聊天状态.
        details: 运行条件、计数、结果与选择信息.
    """
    # dataclass 对象先转换成普通字典, 再与条件和计数一起写入 JSON.
    # run.json 是业务快照; 模型对话轨迹不在这个文件里, 也不提供从快照恢复的接口.
    board = {
        "targets": {key: asdict(value) for key, value in state["targets"].items()},
        "tasks": {key: asdict(value) for key, value in state["tasks"].items()},
        "candidates": {key: asdict(value) for key, value in state["candidates"].items()},
        "results": {key: asdict(value) for key, value in state["results"].items()},
    }
    if "measurements" in state:
        board["measurements"] = {key: asdict(value) for key, value in state["measurements"].items()}
    write_snapshot(directory / "run.json", {**details, "blackboard": board}, temporary_suffix=".json.tmp")
