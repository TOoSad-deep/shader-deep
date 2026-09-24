"""业务校验错误, 保留原公开异常名称."""

from __future__ import annotations


class AnalysisValidationError(ValueError):
    """携带可直接用于局部修复的字段路径, 仍兼容原 ValueError 入口."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        """保留完整错误列表, 文本异常仍可用于既有日志与调用方."""
        self.issues = issues
        super().__init__("; ".join(f"{issue['path']}: {issue['message']}" for issue in issues))


class StaleMaterialError(RuntimeError):
    """材料已变化, 需要后端刷新后重新比较."""
