"""整合角色的固定范围比较执行器, 不继承探索流程."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, cast

from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage

from shader_deep.agents.integration.contracts import CandidateDecision, ControlledWork, FeatureDecision, RelationDecision, SketchDecision
from shader_deep.agents.integration.prompts import CANDIDATE_PROMPT, CONTROLLED_PROMPT, FEATURE_PROMPT, RELATION_PROMPT, SKETCH_PROMPT
from shader_deep.domain.errors import AnalysisValidationError, StaleMaterialError
from shader_deep.domain.library.decisions import IntegrationDecision
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.execution import AnalysisLimitError, AnalysisNoProgressError
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler
from shader_deep.workflows.configuration import resolve_phase_options

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain.agents.middleware import ModelRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage, BaseMessage
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI
    from pydantic import JsonValue

    from shader_deep.domain.library.materials import ComparisonGroup
    from shader_deep.domain.library.models import VisualOutline
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
    from shader_deep.workflows.options import AnalysisOptions

import sys
from copy import deepcopy
from threading import Lock

from langchain_core.messages import ToolMessage

from shader_deep.domain.library.materials import ComparisonManager
from shader_deep.infrastructure.storage.journal import append_tool_record
from shader_deep.infrastructure.tracing import EventLog
from shader_deep.runtime.budgets import RequestBudgetError, check_request
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.history import compact_consumed_history
from shader_deep.runtime.usage import request_sizes

PROTOCOL = "possibility_library_v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


MIN_DEPENDENCY_OWNERS = 2


ComparisonKind = Literal["feature", "relation", "sketch", "candidate"]


class ControlledSession:
    """固定清单每项只运行一次, 待处理台账不会触发自动重入."""

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """复用原会话输入, 附加当前试验结果及已安排身份.

        Args:
            state: 已冻结探索输入的黑板.
            task_id: 原始主任务身份.
            options: 当前试验预算.
            directory: 本次独立运行目录.
            reference_url: 用于验证来源的原图, 不发送给模型.
        """
        self.state, self.task_id, self.options = state, task_id, options
        self.directory, self.reference_url = directory, reference_url
        self.execution = AnalysisExecution()
        self.stop_reason = "running"
        self.outline: VisualOutline | None = None
        self.store: LibraryStore | None = None
        self.outline_versions: list[VisualOutline] = []
        self.issues: list[dict[str, object]] = []
        self.resolutions: dict[str, dict[str, object]] = {}
        self.gaps: list[dict[str, object]] = []
        self.comparisons = ComparisonManager()
        self.lock = Lock()
        self.events = EventLog(directory)
        self.event = self.events.bind(task_id, "integration", self.execution)
        self.group_accepted, self.finish_requested = False, False
        self.integration_cycles = 0
        self.last_action: dict[str, object] = {}
        self.submission_handler: SubmissionHandler | None = None
        self._history_chars = 0
        self._history_messages: list[BaseMessage] = []
        self._consumed_calls: set[str] = set()
        self._last_request: ModelRequest | None = None
        self._context_versions: dict[str, str] = {}
        self._context_text = ""
        self._context_issue_ids: set[str] = set()
        self._accepted_arguments: dict[str, object] | None = None
        self.phase_options = options
        self.phase_budgets: dict[str, dict[str, object]] = {}
        self._tools: list[BaseTool] = []
        self.work_results: list[dict[str, object]] = []
        self._scheduled: set[str] = set()
        self._work: ControlledWork | None = None
        self._phase_limit = 0
        self._phase_scope = "outline"
        self._work_explanation = ""

    def remaining_calls(self) -> int | None:
        """返回全局剩余额度; 独立角色可扣除派发前已消耗的调用."""
        return max(0, self.options.max_main_calls - self.execution.model_calls) if self.options.max_main_calls else None

    def _budget_context(self) -> dict[str, int | None]:
        return {
            "work_calls_remaining": max(0, self._phase_limit - self.execution.model_calls),
            "total_calls_remaining": self.remaining_calls(),
        }

    def _build_context(
        self,
        *,
        extra: dict[str, dict[str, object]] | None = None,
        evidence_ids: list[str] | None = None,
        last_action: dict[str, object] | None = None,
    ) -> HumanMessage:
        if self.outline is None:
            msg = "Integration requires a frozen outline"
            raise ValueError(msg)
        if extra or evidence_ids:
            msg = "Controlled comparison cannot extend materials or request measurements"
            raise ValueError(msg)
        group = self.comparisons.active
        payload = {
            "comparison": group.business_payload() if group else None,
            "submission": self._repair_context(),
            "last_action": self.last_action if last_action is None else last_action,
            "budget": self._budget_context(),
        }
        return HumanMessage(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    def context(self) -> HumanMessage:
        """纯文本呈现完整当前组, 不重复无关初稿、整图或目录."""
        if self.outline is None:
            msg = "Integration requires a frozen outline"
            raise ValueError(msg)
        message = self._build_context()
        self._context_text = str(cast("list[dict[str, object]]", message.content)[0]["text"])
        group = self.comparisons.active
        self._context_versions = dict(group.versions) if group else {}
        self._context_issue_ids = set()
        return message

    def integration_tools(self) -> list[BaseTool]:
        """模型 schema 和执行白名单同时限制在当前比较及其局部修复."""
        schema = self._decision_schema()

        @tool(args_schema=schema)
        def submit_integration(**arguments: object) -> str:
            """提交当前目标的合并、明确保留或整组暂缓; 不允许其他字段."""
            return self._submit_integration(arguments)

        self.submission_handler = SubmissionHandler(
            f"{self.task_id}-controlled-{self._work.id if self._work else 'unknown'}",
            submit_integration,
            self._submit_integration,
            lambda: json.dumps(self.last_action, ensure_ascii=False) if self.group_accepted else None,
            self.lock,
            directory=self.directory,
        )
        return [submit_integration, self.submission_handler.repair_tool()]

    def _submit_integration(self, arguments: dict[str, object]) -> str:
        schema = self._decision_schema()
        decision = schema.model_validate(arguments)
        if decision.deferred_work and (decision.merges or decision.preserve):
            raise AnalysisValidationError(
                [{"path": "/deferred_work", "code": "whole_work_deferral", "message": "Defer the whole work without merges or preserve"}]
            )
        return self._publish_decision(IntegrationDecision.model_validate(decision.model_dump()).model_dump())

    def _decision_schema(self) -> type[FeatureDecision | CandidateDecision | RelationDecision | SketchDecision]:
        if self._work is None:
            msg = "Start a controlled work before submitting its decision"
            raise ValueError(msg)
        return {"feature": FeatureDecision, "candidate": CandidateDecision, "relation": RelationDecision, "sketch": SketchDecision}[self._work.kind]

    def _dependency_reason(self, work: ControlledWork) -> str | None:
        if work.depends_on is None:
            return None
        prior = next((result for result in self.work_results if result["work_id"] == work.depends_on), None)
        if prior is None or prior["status"] != "modified":
            return "dependency_not_merged: " + work.depends_on
        owners = {self._require_store().resolve(identity) for identity in work.owner_ids}
        if len(work.owner_ids) < MIN_DEPENDENCY_OWNERS or len(owners) != 1:
            return "dependency_owners_not_merged: " + work.depends_on
        return None

    def _start_work(self, work: ControlledWork) -> str:
        store = self._require_store()
        targets = list(dict.fromkeys(store.resolve(identity) for identity in work.target_ids))
        entries = store.material_entries()
        if not targets or any(identity not in entries or entries[identity]["kind"] != work.kind for identity in targets):
            msg = "Controlled work requires existing targets of its declared kind"
            raise ValueError(msg)
        if len(targets) > self.options.max_comparison_targets:
            msg = "Controlled work exceeds comparison target limit"
            raise ValueError(msg)
        self._work = work
        self._work_explanation = ""
        group = self.comparisons.start(store, work.question, targets)
        required = store.comparison_materials(targets)
        self.comparisons.append(
            {identity: entries[identity] for identity in required}, store.material_versions(required), library_revision=store.revision
        )
        self.group_accepted, self._accepted_arguments = False, None
        self.submission_handler = None
        self.last_action = {"work_id": work.id, "status": "compare_current_targets"}
        self._history_chars, self._history_messages, self._consumed_calls = 0, [], set()
        self._last_request = None
        return group.id

    def _run_work_phase(self, model: BaseChatModel, *, max_work_calls: int) -> None:
        tools = self.integration_tools()
        scope = self.comparisons.active.id if self.comparisons.active else "controlled"
        self._run_step(model, self._work_prompt(), tools, lambda: self.group_accepted, scope=scope, max_calls=max_work_calls)

    def _run_step(self, model: BaseChatModel, prompt: str, tools: list[BaseTool], done: Callable[[], bool], *, scope: str, max_calls: int) -> None:
        """所有整合步骤共用有限额度、请求准入和按身份累计的修复记录."""
        self.phase_options, source = resolve_phase_options(self.options, "integration")
        self.phase_budgets["integration"] = {"max_output_tokens": self.phase_options.max_output_tokens, "source": source}
        self._tools, self._phase_scope = tools, scope
        self._phase_limit = self.execution.model_calls + max_calls
        remaining = self.remaining_calls()
        if remaining is not None:
            self._phase_limit = min(self._phase_limit, self.execution.model_calls + remaining)
        loop = AnalysisLoop(
            self.execution,
            self._phase_limit,
            self.context,
            done,
            self._tools,
            request_retries=self.options.max_request_retries,
            submission_handler=self.submission_handler,
            on_prepared=self.on_prepared,
            before_request=self._check_request,
            role_prompt_only=True,
            available_tools=self._active_tools,
            on_history=self._record_history,
            prepare_history=self._prepare_history,
            repair_scope=lambda: scope,
            repair_scope_active=lambda active: active == scope,
            on_tool_result=self._save_tool_result,
            on_event=lambda event, details: self.event(
                "phase_checkpoint" if event == "task_completed" else event,
                {"phase": "integration", "work_id": self._work.id if self._work else scope, **details},
            ),
            max_repeated_no_progress=self.options.max_repeated_no_progress or self.options.integration_no_progress,
            progress=lambda: self._require_store().revision,
        )
        self.save()
        loop.run(configure_analysis_model(cast("ChatOpenAI", model), self.phase_options), prompt, self.config(self.task_id))

    def _work_prompt(self) -> str:
        """类型由程序固定, 避免把候选机制标准用于特征外观比较."""
        if self._work is None:
            msg = "Start a controlled work before selecting its comparison criteria"
            raise ValueError(msg)
        criteria = {"feature": FEATURE_PROMPT, "candidate": CANDIDATE_PROMPT, "relation": RELATION_PROMPT, "sketch": SKETCH_PROMPT}[self._work.kind]
        return CONTROLLED_PROMPT + "\n" + criteria

    def _record_history(self, messages: list[BaseMessage]) -> None:
        self._remember_history(messages)
        for message in messages:
            if isinstance(message, AIMessage):
                if self._work is not None and message.content:
                    self._work_explanation = message.text
                self.event("controlled_explanation", {"work_id": self._work.id if self._work else None, "content": message.content})

    def _save_tool_result(self, record: dict[str, JsonValue]) -> None:
        append_tool_record(
            self.directory, self.lock, self.execution.model_calls, {**record, "work_id": self._work.id if self._work else self._phase_scope}
        )

    def save(self) -> None:
        """保存主快照及工作结果, 异常退出也保留已消耗额度和比较回执."""
        destination = self.directory / "integration-work.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.work_results, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _execute_work(self, model: BaseChatModel, work: ControlledWork, *, max_work_calls: int) -> None:
        start = self.execution.model_calls
        self._start_work(work)
        group = cast("ComparisonGroup", self.comparisons.active)
        reason, status = "", ""
        try:
            self._run_work_phase(model, max_work_calls=max_work_calls)
        except (AnalysisLimitError, AnalysisNoProgressError, StaleMaterialError) as exc:
            reason = str(exc)
            if self.comparisons.active is not None:
                self.comparisons.defer(reason)
        except (Exception, KeyboardInterrupt) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            if self.comparisons.active is not None:
                self.comparisons.defer(reason)
            raise
        finally:
            # 服务异常与中断同样保留已发送材料和真实调用数, 然后继续向上抛出原异常.
            self._record_work_result(work, group, start, status=status, reason=reason)

    def _record_work_result(
        self,
        work: ControlledWork,
        group: ComparisonGroup,
        start: int,
        *,
        status: str,
        reason: str,
    ) -> None:
        decision = self._accepted_arguments or {}
        if not status:
            status = "deferred" if not self.group_accepted or decision.get("deferred_work") else "modified" if decision.get("merges") else "preserved"
        self.work_results.append(
            {
                "work_id": work.id,
                "comparison_id": group.id,
                "status": status,
                "reason": reason or "; ".join(cast("list[str]", decision.get("deferred_work", []))),
                "explanation": self._work_explanation,
                "model_calls": self.execution.model_calls - start,
                "target_ids": list(group.target_ids),
                "presented_versions": dict(group.presented_versions),
                "receipt": dict(self.last_action) if self.group_accepted else None,
            }
        )
        self.save()

    @property
    def user_request(self) -> str:
        """返回原始要求与明确约束, 不混入模型判断."""
        target = self.state["targets"][self.state["tasks"][self.task_id].target_version]
        parts = [target.request]
        if target.constraints:
            parts.append("明确约束: " + "; ".join(target.constraints))
        if target.protected_features:
            parts.append("需保护特征: " + "; ".join(target.protected_features))
        objective = self.state["tasks"][self.task_id].objective
        if objective != target.request:
            parts.append("本次分析目标: " + objective)
        return "\n".join(parts)

    def config(self, task_id: str) -> RunnableConfig:
        """构造追踪配置, 子任务和主任务使用独立身份."""
        limit = self.options.max_main_calls if task_id == self.task_id else self.options.max_worker_calls
        return {
            "run_name": "shader-deep.exploration",
            "tags": ["analysis", "coordinator" if task_id == self.task_id else "explorer"],
            "metadata": {"task_id": task_id, "session_id": self.directory.name, "analysis_protocol": PROTOCOL},
            "recursion_limit": sys.maxsize if not limit else limit * 4 + 10,
        }

    def _require_store(self) -> LibraryStore:
        if self.store is None:
            msg = "Submit the visual outline first"
            raise ValueError(msg)
        return self.store

    def _repair_context(self) -> dict[str, object] | None:
        handler = self.submission_handler
        if handler is None or handler.draft is None:
            return None
        if handler.draft.get("status") == "submitted":
            return handler.snapshot()
        return deepcopy(handler.draft)

    def _require_open_group(self) -> None:
        if self.group_accepted or self.stop_reason != "running":
            msg = "Current comparison already ended"
            raise ValueError(msg)

    def _remember_history(self, messages: list[BaseMessage]) -> None:
        """同轮响应参数与工具回执立即占用后续读取额度, 不等待下一次请求."""
        with self.lock:
            self._history_messages.extend(messages)
            self._refresh_history_chars()

    def _normalized_history(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        replaced: set[str] = set()
        draft = self._repair_context()
        if draft and "arguments" in draft:
            replaced = {
                str(call["id"])
                for message in messages
                if isinstance(message, AIMessage)
                for call in message.tool_calls
                if call.get("id")
                and (call["name"] == "submit_integration" or (call["name"] == "repair_analysis_submission" and call["id"] in self._consumed_calls))
            }
        return compact_consumed_history(messages, self._consumed_calls, replaced_draft_call_ids=replaced)

    def _refresh_history_chars(self) -> None:
        if self._last_request is None:
            return
        messages = cast("list[AnyMessage]", self._normalized_history(self._history_messages))
        sizes = request_sizes(self._last_request.override(messages=messages), [])
        self._history_chars = sizes["message_text_chars"] + sizes["image_count"] * self.options.request_image_tokens

    def _prepare_history(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """仅整理请求副本; 实际呈现过的目录可压缩, 正文和当前草稿仍由上下文完整提供."""
        self._history_messages = list(messages)
        self._refresh_history_chars()
        return self._normalized_history(messages)

    def _issue_resolved(self, identity: str) -> bool:
        resolution = self.resolutions.get(identity, {})
        return resolution.get("disposition") == "dismissed" or (
            resolution.get("disposition") == "revised" and resolution.get("verified_outline_version") == len(self.outline_versions)
        )

    def _active_tools(self) -> list[BaseTool]:
        return self._tools

    def _check_request(self, request: ModelRequest) -> None:
        try:
            check_request(request, self._tools, self.phase_options)
        except RequestBudgetError as exc:
            if self.submission_handler and self.submission_handler.draft and self.submission_handler.draft.get("status") != "submitted":
                msg = "repair_context_exceeded: complete draft and required materials do not fit"
                raise RequestBudgetError(msg) from exc
            raise
        self._last_request = request

    def on_prepared(self, messages: list[BaseMessage]) -> None:
        """只登记当前角色实际发送的完整比较材料."""
        self._consumed_calls.update(message.tool_call_id for message in messages if isinstance(message, ToolMessage))
        texts = [
            block.get("text") for message in messages if isinstance(message.content, list) for block in message.content if isinstance(block, dict)
        ]
        if self.comparisons.active is not None and self._context_text in texts:
            self._require_store().present_materials(self._context_versions)
            self.comparisons.mark_presented(self._context_versions)

    def _publish_decision(self, arguments: dict[str, object]) -> str:
        decision = IntegrationDecision.model_validate(arguments)
        if self.group_accepted and arguments == self._accepted_arguments:
            return _json(self.last_action)
        self._require_open_group()
        group = self.comparisons.active
        if group is None:
            msg = "Read a comparison scope before submitting integration"
            raise ValueError(msg)
        if not self.comparisons.is_current(self._require_store(), group):
            msg = "Comparison materials changed; refresh before accepting the decision"
            raise StaleMaterialError(msg)
        self.comparisons.validate_decision(self._require_store(), decision)
        receipt = self._require_store().apply_integration(
            decision,
            submission_id=f"{group.id}-v{group.version}",
            package_id=group.id,
            editable_ids=list(group.target_ids),
            material_versions=group.presented_versions,
            progress={
                **self.comparisons.planned_progress(decision),
                "issue_decisions": [item.model_dump() for item in decision.outline_issue_decisions],
            },
        )
        for item in decision.outline_issue_decisions:
            self.resolutions[item.issue_id] = item.model_dump()
        self.comparisons.accept(self._require_store(), decision, receipt)
        self.group_accepted = True
        self._accepted_arguments = dict(arguments)
        self.last_action = dict(receipt)
        self.save()
        return _json(receipt)
