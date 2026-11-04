"""一次工具绑定中的截图历史, 不评分、不选优、不持有浏览器."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shader_deep.rendering import PreviewSnapshot

_MAX_IMAGES = 3


class PreviewHistory:
    """保留成功截图及其参数快照, 编号按记录顺序从 1 开始."""

    def __init__(self) -> None:
        """每次构建工具获得独立历史, 不跨 Agent 或参考图共享."""
        self._snapshots: list[PreviewSnapshot] = []
        self._lock = Lock()

    def record(self, snapshot: PreviewSnapshot) -> int:
        """记录不可变快照, 返回截图编号; 同参数重复截图也产生新编号."""
        with self._lock:
            self._snapshots.append(snapshot)
            return len(self._snapshots)

    def read(self, trial_ids: list[int] | None) -> tuple[int, list[tuple[int, PreviewSnapshot]]]:
        """读取最近三张或指定至多三张原图, 不再次渲染或改变预览.

        Args:
            trial_ids: 截图编号, None 表示最近三张; 显式空列表无效.

        Returns:
            总截图数及按所选顺序排列的编号与快照.

        Raises:
            ValueError: 请求为空、超过三张、编号重复或不存在.
        """
        with self._lock:
            count = len(self._snapshots)
            if trial_ids is None:
                trial_ids = list(range(max(1, count - _MAX_IMAGES + 1), count + 1))
            elif not 1 <= len(trial_ids) <= _MAX_IMAGES or len(set(trial_ids)) != len(trial_ids):
                msg = "Select one to three distinct trial IDs"
                raise ValueError(msg)
            if any(type(number) is not int or not 1 <= number <= count for number in trial_ids):
                msg = "Unknown trial ID; only successful captures have an ID"
                raise ValueError(msg)
            return count, [(number, self._snapshots[number - 1]) for number in trial_ids]
