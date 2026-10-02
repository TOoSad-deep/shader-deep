"""主 Agent 只发起业务动作, 协调器拥有任务、结果与发布状态."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shader_deep.domain.five_libraries import Element
    from shader_deep.runtime.task_store import JsonValue


class MainAgentActions(Protocol):
    """运行宿主提供的本轮能力, 回执成功不表示主会话已结束."""

    def submit_elements(self, elements: tuple[Element, ...]) -> dict[str, JsonValue]:
        """登记独立元素目录并返回稳定引用."""
        ...

    def dispatch_exploration(self, target_element_id: str, directions: tuple[str, ...]) -> dict[str, JsonValue]:
        """执行固定目标的一批探索, 内部有限恢复后返回状态."""
        ...

    def read_analysis_result(self, result_id: str) -> dict[str, JsonValue]:
        """读取本轮已接受的结果正文."""
        ...

    def dispatch_integration(self) -> dict[str, JsonValue]:
        """汇集合法 V0 并执行独立整合."""
        ...

    def finish_analysis(self) -> dict[str, JsonValue]:
        """提交程序计算的最终交付结果."""
        ...

    def allowed_tools(self) -> tuple[str, ...]:
        """返回当前业务状态允许的工具名称."""
        ...

    def progress(self) -> object:
        """返回接受的动作与实际读取形成的进展身份."""
        ...

    def context(self) -> dict[str, JsonValue]:
        """提供登记目录、任务状态和合法结果引用."""
        ...

    def done(self) -> bool:
        """最终结果提交后才结束持续主会话."""
        ...
