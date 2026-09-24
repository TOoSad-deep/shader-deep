"""整合子 Agent 的显式调用边界, 组合角色执行程序而不继承探索会话."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.agents.integration.stages import IntegrationProgram

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.agents.integration.worker import IntegrationInput, IntegrationOutcome
    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.workflows.options import AnalysisOptions


class IntegrationSubagent:
    """仅暴露启动、进度身份和结构化结果, 内部会话不进入工作流."""

    def __init__(
        self,
        inputs: IntegrationInput,
        options: AnalysisOptions,
        directory: Path,
        reference_url: str,
        *,
        on_progress: Callable[[IntegrationOutcome], None] | None = None,
    ) -> None:
        """装配独立整合程序, 保留原子角色的输入契约."""
        self._program = IntegrationProgram(inputs, options, directory, reference_url, on_progress=on_progress)

    @property
    def execution(self) -> AnalysisExecution:
        """当前角色的执行计数与终态."""
        return self._program.execution

    @property
    def agent_id(self) -> str:
        """用于事件及模型追踪的独立身份."""
        return self._program.agent_id

    def run(self, model: BaseChatModel) -> None:
        """同步执行整合, 异常保留业务进度后向调用方传播."""
        self._program.run(model)

    def result(self) -> IntegrationOutcome:
        """返回复制后的业务进度, 不共享历史和草稿."""
        return self._program.result()
