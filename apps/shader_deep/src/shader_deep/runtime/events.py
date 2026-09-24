"""记录离散执行事件, 并可选地识别完全相同的失败循环."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from shader_deep.runtime.execution import AnalysisNoProgressError

if TYPE_CHECKING:
    from pydantic import JsonValue


EventCallback = Callable[[str, dict[str, "JsonValue"]], None]


def failure_signature(tool: str, arguments: object, issues: object) -> str:
    """计算失败内容指纹, 不把完整参数写入事件或错误信息.

    Args:
        tool: 原提交工具名; 修复提交仍使用原工具身份.
        arguments: 实际失败的参数对象, 不包含草稿版本等运行元数据.
        issues: 稳定的错误类别、位置或消息.

    Returns:
        规范化内容的摘要.
    """
    content = json.dumps([tool, arguments, issues], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


class NoProgressGuard:
    """仅识别连续同错同内容, 默认关闭, 不判断分析语义质量."""

    def __init__(self, limit: int = 0) -> None:
        """绑定停止阈值.

        Args:
            limit: 连续重复失败轮数阈值; 0 表示关闭.
        """
        self.limit = limit
        self.last: tuple[str, str] | None = None
        self.repeated = 0

    def check(self, signature: str | None, progress: object) -> None:
        """在下一模型请求前检查上一轮失败及新材料变化.

        Args:
            signature: 上一轮失败指纹; 无拒绝时为空.
            progress: 新报告、测量和实际已读材料的稳定计数或标识.

        Raises:
            AnalysisNoProgressError: 已达显式配置的连续无进展阈值.
        """
        if not self.limit or signature is None:
            self.last, self.repeated = None, 0
            return
        current = (signature, json.dumps(progress, sort_keys=True, default=str))
        self.repeated = self.repeated + 1 if current == self.last else 1
        self.last = current
        if self.repeated >= self.limit:
            msg = f"Analysis stopped after {self.repeated} identical rejected turns without new material"
            raise AnalysisNoProgressError(msg)
