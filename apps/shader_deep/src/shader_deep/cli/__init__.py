"""命令行入口; 延迟保留旧 main 导出, 避免模块启动时重复导入."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shader_deep.cli.generation import main as main, parse_args as parse_args


def __getattr__(name: str) -> object:
    """按旧名称读取生成 CLI 入口."""
    return getattr(import_module("shader_deep.cli.generation"), name)
