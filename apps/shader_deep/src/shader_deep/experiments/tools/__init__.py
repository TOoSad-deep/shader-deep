"""独立调参实验的工具入口, 浏览器生命周期由调用方管理."""

from shader_deep.experiments.tools.factory import build_parameter_tools

__all__ = ["build_parameter_tools"]
