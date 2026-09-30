"""复用模型传输和工具白名单, 按真实进展约束五库角色循环."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.runtime.execution import AnalysisLimitError, AnalysisNoProgressError
from shader_deep.runtime.runner import AnalysisLoop

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse


MAX_IDLE_TURNS = 3


class FiveAnalysisLoop(AnalysisLoop):
    """任何连续三轮无新读取或有效提交均停止, 不仅检查同一错误."""

    repair_charge: Callable[[], object]
    model_charge: Callable[[], int]
    inactive_check: Callable[[], None]
    previous_progress: object = None
    idle_turns: int = 0

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse[object]]) -> ModelResponse[object]:
        """在下一请求前检查上轮进展, 请求内部网络重试不增加停滞次数."""
        if self.done():
            return super().wrap_model_call(request, handler)
        self.inactive_check()
        current = self.progress() if self.progress is not None else None
        if self.execution.model_calls:
            self.idle_turns = self.idle_turns + 1 if current == self.previous_progress else 0
        self.previous_progress = current
        if self.idle_turns >= MAX_IDLE_TURNS:
            msg = "连续三次模型响应没有有效新读取或接受的提交"
            raise AnalysisNoProgressError(msg)
        return super().wrap_model_call(request, handler)

    def _pending_repair(self) -> str | None:
        """引用及业务拒绝与格式错误共用逻辑任务的持久修复额度."""
        errors = [
            item
            for item in self.execution.tool_feedback
            if item.model_call == self.execution.model_calls and item.category in {"json_syntax", "schema_validation", "business_rejection"}
        ]
        return self.repair_scope() if errors and self.repair_scope is not None else None

    def _attempt_guard(self, prepared: ModelRequest, repair: str | None) -> Callable[[], None]:
        """修复次数在实际网络发送前扣除, 网络重试不重复扣除."""
        original = super()._attempt_guard(prepared, repair)
        charged = False
        called = False

        def before_attempt() -> None:
            nonlocal charged, called
            self.inactive_check()
            if repair is not None and not charged:
                self.repair_charge()
                charged = True
            original()
            if not called:
                self._charge_call()
                called = True

        return before_attempt

    def _charge_call(self) -> None:
        try:
            self.model_charge()
        except ValueError as error:
            if "逻辑模型调用额度已耗尽" not in str(error):
                raise
            raise AnalysisLimitError(str(error)) from error
