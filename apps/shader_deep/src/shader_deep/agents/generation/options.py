"""生成角色的渲染条件与尝试额度."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class GenerationOptions:
    """单次生成运行的渲染条件和预算.

    Attributes:
        width: 每个候选的固定渲染宽度.
        height: 每个候选的固定渲染高度.
        time: 固定传给 iTime 的秒数.
        max_attempts: 最多提交多少个渲染候选, 失败也计数.
        output_dir: 运行目录的父目录, 未提供时使用当前目录下的 runs.
    """

    width: int = 512
    height: int = 512
    time: float = 0.0
    max_attempts: int = 3
    output_dir: Path | None = None

    def __post_init__(self) -> None:
        """在启动模型或浏览器前检查预算与渲染条件."""
        # dataclass 初始化后自动调用; 尽早拒绝无效配置, 避免发起无意义的模型请求.
        # bool 是 int 的子类, 因此必须单独排除 True/False 作为尺寸或次数.
        for name in ("width", "height", "max_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                msg = f"{name} must be a positive integer"
                raise ValueError(msg)
        if isinstance(self.time, bool) or not isinstance(self.time, (int, float)) or not math.isfinite(self.time):
            msg = "time must be a finite number of seconds"
            raise ValueError(msg)
