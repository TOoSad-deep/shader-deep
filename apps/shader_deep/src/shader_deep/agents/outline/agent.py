"""并行探索直接入库, 主 Agent 按需选材并批量整合与交付."""

from __future__ import annotations

import json
from threading import Lock
from typing import TYPE_CHECKING, cast

from langchain.tools import tool

from shader_deep.agents.outline.context import build_outline_context
from shader_deep.agents.outline.prompts import OUTLINE_PROMPT
from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.library.models import VisualOutline  # noqa: TC001  # 工具装饰器运行时解析初稿参数.
from shader_deep.domain.library.validation import validate_outline
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.budgets import check_request
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler
from shader_deep.workflows.options import MIN_ANALYSIS_TASKS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI

    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.workflows.options import AnalysisOptions


from shader_deep.infrastructure.storage.journal import append_tool_record

if TYPE_CHECKING:
    from shader_deep.runtime.events import EventCallback


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class OutlineAgent:
    """仅生成初稿和探索方向, 通过回调报告进度."""

    def __init__(
        self,
        request: str,
        reference_url: str,
        options: AnalysisOptions,
        directory: Path,
        task_id: str,
        execution: AnalysisExecution,
        on_progress: Callable[[VisualOutline | None, list[str], str], None],
        event: EventCallback,
    ) -> None:
        """绑定固定输入与工作流提供的初稿执行记录."""
        self.user_request, self.reference_url, self.options = request, reference_url, options
        self.directory, self.task_id, self.execution = directory, task_id, execution
        self._on_progress, self.event = on_progress, event
        self.outline: VisualOutline | None = None
        self.directions: list[str] = []
        self.stop_reason = "running"
        self.group_accepted = False
        self.last_action: dict[str, object] = {}
        self.lock = Lock()
        self.submission_handler: SubmissionHandler | None = None

    def save(self) -> None:
        """将业务结果报告给工作流, 不直接写主快照."""
        self._on_progress(self.outline, self.directions, self.stop_reason)

    def run(self, model: BaseChatModel, config: RunnableConfig) -> None:
        """在初稿额度内执行原有提交与修复协议."""
        tools = self.outline_tools()
        limit = self.execution.model_calls + self.options.max_integration_calls
        if self.options.max_main_calls:
            limit = min(limit, self.options.max_main_calls)
        loop = AnalysisLoop(
            self.execution,
            limit,
            lambda: build_outline_context(self.user_request, self.reference_url),
            lambda: self.outline is not None or self.stop_reason != "running",
            tools,
            request_retries=self.options.max_request_retries,
            submission_handler=self.submission_handler,
            role_prompt_only=True,
            before_request=lambda request: check_request(request, tools, self.options),
            repair_scope=lambda: "outline",
            repair_scope_active=lambda scope: scope == "outline" and self.outline is None,
            on_event=lambda event, details: self.event("phase_checkpoint" if event == "task_completed" else event, {"phase": "outline", **details}),
            on_tool_result=lambda record: append_tool_record(self.directory, self.lock, self.execution.model_calls, record),
            max_repeated_no_progress=self.options.max_repeated_no_progress or self.options.integration_no_progress,
        )
        loop.run(configure_analysis_model(cast("ChatOpenAI", model), self.options), OUTLINE_PROMPT, config)

    def _submit_outline(self, outline: VisualOutline, directions: list[str]) -> str:
        if self.stop_reason != "running":
            return _json(self.last_action)
        if self.outline is not None:
            if self.outline == outline and self.directions == directions:
                return _json(self.last_action)
            msg = "Accepted outline and exploration directions cannot be replaced"
            raise ValueError(msg)
        try:
            validate_outline(outline)
        except AnalysisValidationError as exc:
            raise AnalysisValidationError([{**issue, "path": "/outline" + str(issue["path"])} for issue in exc.issues]) from exc
        if not MIN_ANALYSIS_TASKS <= len(directions) <= self.options.max_tasks or any(not value.strip() for value in directions):
            raise AnalysisValidationError(
                [{"path": "/directions", "code": "invalid_directions", "message": "Provide two or more nonblank directions within task budget"}]
            )
        self.outline, self.directions = outline, directions
        self.last_action = {"status": "outline_submitted", "tasks": len(directions)}
        self.save()
        return _json(self.last_action)

    def outline_tools(self) -> list[BaseTool]:
        """初稿和独立探索方向一次提交, 后端负责派发."""

        @tool
        def submit_visual_outline(outline: VisualOutline, directions: list[str]) -> str:
            """提交中性初稿与至少两个开放探索问题, 本轮随后冻结元素身份.

            Args:
                outline: 统一视觉元素、实例组和可见关系.
                directions: 独立探索问题, 不预设机制答案.
            """
            return self._submit_outline(outline, directions)

        self.submission_handler = SubmissionHandler(
            self.task_id + "-outline",
            submit_visual_outline,
            lambda args: self._submit_outline(cast("VisualOutline", args["outline"]), cast("list[str]", args["directions"])),
            lambda: _json(self.last_action) if self.stop_reason != "running" else None,
            self.lock,
            directory=self.directory,
        )
        return [submit_visual_outline, self.submission_handler.repair_tool(), self._stop_tool()]

    def _stop_tool(self) -> BaseTool:
        @tool
        def stop_analysis(reason: str) -> str:
            """停止当前运行并保存已有结果和具体缺口.

            Args:
                reason: 无法继续的原因.
            """
            with self.lock:
                if self.group_accepted or self.stop_reason != "running":
                    return _json(self.last_action)
                if not reason.strip():
                    msg = "Provide a concrete reason for stopping"
                    raise ValueError(msg)
                self.stop_reason, self.execution.error = "stopped", reason
                self.last_action = {"status": "stopped", "reason": reason}
                self.save()
                return _json(self.last_action)

        return stop_analysis
