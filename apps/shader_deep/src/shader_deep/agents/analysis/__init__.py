"""当前原生分析角色及按需加载的公开工作流入口."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shader_deep.workflows.analysis import run_analysis as run_analysis, run_analysis_task as run_analysis_task

__all__ = ["run_analysis", "run_analysis_task"]


def __getattr__(name: str) -> object:
    """仅在访问公开运行入口时加载工作流, 避免初始化反向依赖."""
    if name in __all__:
        from shader_deep.workflows import analysis  # noqa: PLC0415  # 兼容入口按需加载, 避免包初始化循环.

        return getattr(analysis, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
