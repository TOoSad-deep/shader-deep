"""并行探索直接入库, 主 Agent 按需选材并批量整合与交付."""

from __future__ import annotations

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from langchain.tools import tool
from langchain_core.messages import AIMessage, ToolMessage

from shader_deep.analysis.config import resolve_phase_options
from shader_deep.analysis.context_views import bounded_context_page
from shader_deep.analysis.events import EventLog
from shader_deep.analysis.evidence import MeasurementRequest  # noqa: TC001  # 工具 schema 在运行时解析参数类型.
from shader_deep.analysis.exploration import ExplorationReport, VisualOutline, compact_data, validate_outline
from shader_deep.analysis.exploration_prompts import INTEGRATION_PROMPT, OUTLINE_PROMPT
from shader_deep.analysis.exploration_worker import ExplorationOutcome, run_exploration
from shader_deep.analysis.history import compact_consumed_history
from shader_deep.analysis.integration import IntegrationDecision
from shader_deep.analysis.library import LibraryStore
from shader_deep.analysis.library_views import ListComparisonWorkInput, ListLibraryInput, ReadLibraryInput, list_library, select_library
from shader_deep.analysis.loop import AnalysisLoop
from shader_deep.analysis.materials import ComparisonManager
from shader_deep.analysis.measurements import ReferenceMeasurements
from shader_deep.analysis.request_budget import RequestBudgetError, check_request, remaining_material_chars
from shader_deep.analysis.schemas import AnalysisValidationError, LensConfig
from shader_deep.analysis.submissions import SubmissionHandler
from shader_deep.analysis.transport import configure_analysis_model
from shader_deep.analysis.types import MIN_ANALYSIS_TASKS, AnalysisExecution, AnalysisLimitError, AnalysisNoProgressError, AnalysisOutcome
from shader_deep.analysis.usage import request_sizes
from shader_deep.artifacts import save_run
from shader_deep.blackboard import add_result, add_task
from shader_deep.context.analysis import _evidence_payload
from shader_deep.context.common import png_data_url
from shader_deep.context.exploration import build_integration_context, build_outline_context
from shader_deep.schemas import ResultRecord, TaskRecord

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from langchain.agents.middleware import ModelRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI
    from pydantic import JsonValue

    from shader_deep.analysis.types import AnalysisOptions
    from shader_deep.schemas import BlackboardState

PROTOCOL = "possibility_library_v1"
MAX_CONTEXT_PAGE_SIZE = 100
FutureResult = TypeVar("FutureResult")


class StaleMaterialError(RuntimeError):
    """材料已变化, 交由后端刷新当前组而非让模型反复修复旧参数."""


class RepairContextError(RequestBudgetError):
    """完整修复草稿与必要材料无法同轮呈现, 保留制品并暂缓本组."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class ExplorationSession:
    """独立探索后按需选择比较范围, 同组修复保留模型历史."""

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """绑定固定输入与运行目录.

        Args:
            state: 初始黑板.
            task_id: 主任务身份.
            options: 运行与请求预算.
            directory: 当前独立运行目录.
            reference_url: 启动时固定的原图内容.
        """
        self.state, self.task_id, self.options = state, task_id, options
        self.directory, self.reference_url = directory, reference_url
        self.execution = AnalysisExecution()
        self.workers: dict[str, AnalysisExecution] = {}
        self.summary_result: ResultRecord | None = None
        self.stop_reason = "running"
        self.outline: VisualOutline | None = None
        self.store: LibraryStore | None = None
        self.issues: list[dict[str, object]] = []
        self.resolutions: dict[str, dict[str, object]] = {}
        self.lock = Lock()
        self.cancelled = Event()
        self.last_action: dict[str, object] = {}
        self.selected_evidence: list[str] = []
        self.presented_evidence: set[str] = set()
        self.submission_handler: SubmissionHandler | None = None
        self.events = EventLog(directory)
        self.event = self.events.bind(task_id, "coordinator", self.execution)
        self.measurements = ReferenceMeasurements(
            base64.b64decode(reference_url.split(",", 1)[1]), directory, task_id, state["tasks"][task_id].target_version, options.max_measurements
        )
        self.outline_versions: list[VisualOutline] = []
        self.directions: list[str] = []
        self.comparisons = ComparisonManager()
        self.group_accepted = False
        self.integration_cycles = 0
        self.finish_requested = False
        self._history_chars = 0
        self._last_request: ModelRequest | None = None
        self._context_versions: dict[str, str] = {}
        self._accepted_arguments: dict[str, object] | None = None
        self.gaps: list[dict[str, object]] = []
        self._context_text = ""
        self._tools: list[BaseTool] = []
        self.phase_options = options
        self.phase_budgets: dict[str, dict[str, object]] = {}
        self._history_messages: list[BaseMessage] = []
        self._consumed_calls: set[str] = set()
        self._context_issue_ids: set[str] = set()
        self.presented_issues: set[str] = set()

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

    def _control(self, *, last_action: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "comparison_target_limit": self.options.max_comparison_targets,
            "measurement_ids": list(self.state.get("measurements", {})),
            "measurements_remaining": self.options.max_measurements - len(self.state.get("measurements", {})),
            "last_action": self.last_action if last_action is None else last_action,
            "submission": self._repair_context(),
        }

    def _repair_context(self) -> dict[str, object] | None:
        handler = self.submission_handler
        if handler is None or handler.draft is None:
            return None
        if handler.draft.get("status") == "submitted":
            return handler.snapshot()
        return deepcopy(handler.draft)

    def _integration_payload(self, extra: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
        group = self.comparisons.active
        comparison = group.business_payload() if group else None
        if group is not None and comparison is not None and extra:
            comparison = {**comparison, "entries": list({**group.entries, **extra}.values())}
        return {
            "catalog": (
                {"entries": [], "message": "Use list_library for additional identities"}
                if group
                else list_library(
                    self._require_store(),
                    processing_status=self.comparisons.status_summary(self._require_store()),
                    max_chars=self.options.library_page_chars,
                )
            ),
            "comparison": comparison,
            "progress": self.comparisons.summary(max_chars=self.options.library_page_chars),
            "context": self._material_context(),
        }

    def _build_context(
        self,
        *,
        extra: dict[str, dict[str, object]] | None = None,
        evidence_ids: list[str] | None = None,
        last_action: dict[str, object] | None = None,
    ) -> HumanMessage:
        if self.outline is None:
            return build_outline_context(self.user_request, self.reference_url)
        payload = self._integration_payload(extra)
        message = build_integration_context(
            self.user_request, self.outline, self.reference_url, index=payload, control=self._control(last_action=last_action)
        )
        blocks = list(message.content) if isinstance(message.content, list) else [{"type": "text", "text": message.content}]
        for identity in self.selected_evidence if evidence_ids is None else evidence_ids:
            record = self.state["measurements"][identity]
            blocks.append({"type": "text", "text": _json({"measurement": _evidence_payload(record)})})
            if record.artifact_path is not None:
                blocks.append({"type": "image_url", "image_url": {"url": png_data_url(Path(record.artifact_path))}})
        return message.model_copy(update={"content": blocks})

    def context(self) -> HumanMessage:
        """只提供索引、当前组正文和选中测量, 不把正文累加到历史."""
        message = self._build_context()
        blocks = cast("list[dict[str, object]]", message.content)
        self._context_text = str(blocks[0]["text"])
        group = self.comparisons.active
        self._context_versions = dict(group.versions) if group else {}
        self._context_issue_ids = {
            str(issue["id"])
            for issue in cast("list[dict[str, object]]", self._context_page("outline_issues")["entries"])
            if not issue.get("details_unavailable_due_to_budget")
        }
        return message

    def on_prepared(self, messages: list[BaseMessage]) -> None:
        """只有正文实际进入已通过预算检查的请求才登记呈现."""
        self._consumed_calls.update(message.tool_call_id for message in messages if isinstance(message, ToolMessage))
        self._present_issue_pages(messages)
        texts = [
            block.get("text") for message in messages if isinstance(message.content, list) for block in message.content if isinstance(block, dict)
        ]
        if self.comparisons.active is not None and self._context_text in texts:
            self.presented_issues.update(self._context_issue_ids)
            self._require_store().present_materials(self._context_versions)
            self.comparisons.mark_presented(self._context_versions)
            self._present_measurements(messages, texts)

    def _present_issue_pages(self, messages: list[BaseMessage]) -> None:
        for message in messages:
            if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
                continue
            try:
                page = json.loads(message.content)
            except ValueError:
                continue
            if isinstance(page, dict) and page.get("section") == "outline_issues":
                self.presented_issues.update(
                    str(item["id"]) for item in page.get("entries", []) if "id" in item and not item.get("details_unavailable_due_to_budget")
                )

    def _present_measurements(self, messages: list[BaseMessage], texts: list[object]) -> None:
        images = [
            block.get("image_url")
            for message in messages
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, dict)
        ]
        for identity in self.selected_evidence:
            record = self.state["measurements"][identity]
            if _json({"measurement": _evidence_payload(record)}) not in texts:
                continue
            if record.artifact_path is None or {"url": png_data_url(Path(record.artifact_path))} in images:
                self.presented_evidence.add(identity)

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
        self.outline_versions.append(outline)
        self.store = LibraryStore(outline, self.directory)
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

    def _register(self) -> list[TaskRecord]:
        tasks = []
        for offset, direction in enumerate(self.directions, start=1):
            identifier = f"{self.directory.name}-a{offset:03d}"
            task = TaskRecord(
                id=identifier,
                role="analysis",
                target_version=self.state["tasks"][self.task_id].target_version,
                objective=direction,
                parent_task_id=self.task_id,
                lens_config=LensConfig(id=identifier + "-direction", origin="generated", name="独立探索", focus=direction),
                analysis_outline=self.outline,
                analysis_purpose="initial",
            )
            self.state = add_task(self.state, task)
            self.workers[task.id] = AnalysisExecution()
            tasks.append(task)
        self.save()
        return tasks

    def _commit_report(self, task: TaskRecord, report: ExplorationReport) -> dict[str, object]:
        # 子任务模型请求期间不持会话锁, 这里只串行发布一次完整提交.
        with self.lock:
            if self.cancelled.is_set():
                msg = "Exploration cancelled before publication"
                raise ValueError(msg)
            identity = task.id + "-report"
            self._require_store().add_report(identity, report)
            if identity not in self.state["results"]:
                self.state = add_result(
                    self.state,
                    ResultRecord(
                        id=identity,
                        task_id=task.id,
                        status="completed",
                        summary="独立探索已入库",
                        analysis_detail=report,
                        analysis_protocol=PROTOCOL,
                    ),
                )
            self.save()
            return {"status": "stored", "result_id": identity}

    def _collect(self, task: TaskRecord, outcome: ExplorationOutcome) -> None:
        # 成功回执之前已经入库; 收集只合并运行状态和问题, 不重复发布报告.
        with self.lock:
            self.workers[task.id] = outcome.execution
            for item in outcome.issues:
                self.issues.append({"id": str(len(self.issues) + 1), "outline_version": 1, "task_id": task.id, **asdict(item)})
            if task.id + "-report" not in self.state["results"]:
                self.state = add_result(
                    self.state,
                    ResultRecord(
                        id=task.id + "-failure",
                        task_id=task.id,
                        status="blocked",
                        summary=outcome.error or "未形成可交付候选",
                        analysis_protocol=PROTOCOL,
                    ),
                )
            self.save()

    def _run_batch(self) -> None:
        tasks = self._register()
        with ThreadPoolExecutor(max_workers=self.options.max_parallel, thread_name_prefix="exploration") as executor:
            futures = {
                executor.submit(
                    copy_context().run,
                    run_exploration,
                    self.user_request,
                    cast("VisualOutline", self.outline),
                    task.objective,
                    self.reference_url,
                    options=self.options,
                    config=self.config(task.id),
                    directory=self.directory,
                    event_log=self.events,
                    task_id=task.id,
                    commit_report=self._committer(task),
                    should_stop=self.cancelled.is_set,
                ): task
                for task in tasks
            }
            collected: set[str] = set()
            try:
                for future in as_completed(futures):
                    self._collect_future(futures[future], future)
                    collected.add(futures[future].id)
            except KeyboardInterrupt:
                self.cancelled.set()
                with self.lock:
                    self.stop_reason, self.execution.status = "interrupted", "stopped"
                    self.save()
                for future in futures:
                    future.cancel()
                # 在途请求仍受网络超时控制, 返回后取消标记阻止新请求和新提交.
                for future in as_completed(futures):
                    if futures[future].id not in collected:
                        self._collect_future(futures[future], future)
                raise

    def _collect_future(self, task: TaskRecord, future: Future[FutureResult]) -> None:
        try:
            outcome = cast("ExplorationOutcome", future.result())
        except Exception as exc:  # noqa: BLE001  # 保留单个线程失败, 不丢弃兄弟报告.
            execution = AnalysisExecution(status="stopped" if self.cancelled.is_set() else "failed", error=str(exc))
            outcome = ExplorationOutcome(report=None, execution=execution, issues=(), error=str(exc))
        self._collect(task, outcome)

    def _committer(self, task: TaskRecord) -> Callable[[ExplorationReport], dict[str, object]]:
        def commit(report: ExplorationReport) -> dict[str, object]:
            return self._commit_report(task, report)

        return commit

    def _uncovered(self) -> list[str]:
        library = self._require_store().library
        covered = {identity for sketch in library.sketch_library for identity in sketch.element_ids}
        covered.update(identity for feature in library.feature_library for identity in feature.element_ids)
        covered.update(endpoint.id for relation in library.relation_library for endpoint in relation.participants if endpoint.kind == "element")
        return [element.id for element in library.elements if element.id not in covered]

    def _material_context(self) -> dict[str, object]:
        sections = ("outline_issues", "failed_tasks", "uncovered_elements")
        pages = {section: self._context_page(section) for section in sections}
        return {
            **{section: page["entries"] for section, page in pages.items()},
            "pages": {key: {k: v for k, v in page.items() if k != "entries"} for key, page in pages.items()},
        }

    def _context_page(self, section: str, *, cursor: str | None = None, limit: int = 30) -> dict[str, object]:
        rows: list[dict[str, object]]
        if section == "outline_issues":
            rows = [issue for issue in self.issues if self.resolutions.get(str(issue["id"]), {}).get("disposition") != "dismissed"]
        elif section == "failed_tasks":
            rows = [{"id": key, "status": value.status, "reason": value.error} for key, value in self.workers.items() if value.status != "completed"]
        else:
            rows = [{"id": identity} for identity in self._uncovered()]
        return bounded_context_page(section, rows, cursor=cursor, limit=limit, max_chars=self.options.library_page_chars)

    def _validate_issues(self, decision: IntegrationDecision) -> None:
        known = {str(issue["id"]) for issue in self.issues}
        for offset, item in enumerate(decision.outline_issue_decisions):
            if item.issue_id not in known or item.issue_id not in self.presented_issues or not item.reason.strip():
                raise AnalysisValidationError(
                    [
                        {
                            "path": f"/outline_issue_decisions/{offset}/issue_id",
                            "code": "invalid_issue",
                            "message": "Use a presented issue ID and provide a concrete reason",
                        }
                    ]
                )

    def _submit_integration(self, arguments: dict[str, object]) -> str:
        decision = IntegrationDecision.model_validate(arguments)
        if self.group_accepted and arguments == self._accepted_arguments:
            return _json(self.last_action)
        self._require_open_group()
        group = self.comparisons.active
        if group is None:
            msg = "Read a comparison scope before submitting integration"
            raise ValueError(msg)
        self._validate_issues(decision)
        if set(self.selected_evidence) - self.presented_evidence:
            msg = "Receive all requested measurements before submitting integration"
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

    def _require_open_group(self) -> None:
        if self.group_accepted or self.stop_reason != "running":
            msg = "Current comparison already ended"
            raise ValueError(msg)

    def _read_library(self, arguments: dict[str, object]) -> str:
        self._require_open_group()
        request = ReadLibraryInput.model_validate(arguments)
        store = self._require_store()
        entries = select_library(store, request.requests)
        original = self.comparisons
        # 工具调用持会话锁; 所有预算预演只修改工作副本, 拒绝不能留下范围或版本变化.
        self.comparisons = deepcopy(original)
        try:
            result = self._stage_read(request, entries)
        except Exception:
            self.comparisons = original
            raise
        self.last_action = result
        self.save()
        return _json(result)

    def _stage_read(self, request: ReadLibraryInput, entries: dict[str, dict[str, object]]) -> dict[str, object]:
        store = self._require_store()
        scope_changed = self.comparisons.active is None or bool(request.extend_target_ids)
        if self.comparisons.active is None:
            specification = request.comparison
            if specification is None and request.work_id is None:
                msg = "First read requires comparison or an existing work_id"
                raise ValueError(msg)
            self.comparisons.start(
                store, specification.question if specification else "", specification.target_ids if specification else [], work_id=request.work_id
            )
        elif request.comparison is not None or request.work_id is not None:
            msg = "An active comparison cannot be replaced; append reads or extend_target_ids"
            raise ValueError(msg)
        if request.extend_target_ids:
            self.comparisons.extend_targets(store, request.extend_target_ids)
        group = self.comparisons.active
        if group is None:
            msg = "No active comparison after scope selection"
            raise ValueError(msg)
        if len(group.target_ids) > self.options.max_comparison_targets:
            msg = f"target_scope_exceeded: limit {self.options.max_comparison_targets}; keep required work pending or deferred"
            raise ValueError(msg)
        required = store.comparison_materials(list(group.target_ids)) if scope_changed else []
        versions = store.material_versions(required)
        missing = [identity for identity in required if group.versions.get(identity) != versions[identity] and identity not in entries]
        if missing:
            msg = f"target_materials_missing: explicitly request complete target materials: {missing[:12]} (total {len(missing)})"
            raise ValueError(msg)
        # 三种 ID 列表始终构成请求集合的分区; 全部放入一个列表并带拒绝原因,
        # 是本次回执的保守大小上界, 在选材前同时预留历史与控制上下文中的副本.
        bound = self._read_receipt([], [], list(entries))
        if not self._fits_context(receipt=bound):
            msg = "Read receipt exceeds remaining budget; no new bodies selected. Continue this comparison with fewer ids or candidate_ids."
            raise ValueError(msg)
        required_entries = {identity: entries[identity] for identity in required if identity in entries}
        ordered = {**required_entries, **entries}
        selected, duplicate, omitted = self._admit_entries(ordered, receipt=bound)
        if set(required).intersection(omitted):
            msg = "target_materials_exceeded: complete target materials do not fit; scope and selection unchanged"
            raise ValueError(msg)
        return self._read_receipt(selected, duplicate, omitted)

    @staticmethod
    def _read_receipt(selected: list[str], duplicate: list[str], omitted: list[str]) -> dict[str, object]:
        """完整列明三个互斥范围, 同一形状用于准入预算和最终回执."""
        return {
            "status": "selected" if selected or duplicate else "not_selected",
            "selected_ids": selected,
            "duplicate_ids": duplicate,
            "not_provided_ids": omitted,
            "reason": "input_budget_exceeded" if omitted else None,
        }

    def _admit_entries(self, entries: dict[str, dict[str, object]], *, receipt: dict[str, object]) -> tuple[list[str], list[str], list[str]]:
        group = self.comparisons.active
        if group is None:
            msg = "No active comparison"
            raise ValueError(msg)
        selected, duplicate, omitted = [], [], []
        for identity, entry in entries.items():
            version = self._require_store().material_versions([identity])
            if group.versions.get(identity) == version[identity]:
                duplicate.append(identity)
            elif self._fits_context(extra={identity: entry}, receipt=receipt):
                self.comparisons.append({identity: entry}, version, library_revision=self._require_store().revision)
                selected.append(identity)
            else:
                omitted.append(identity)
        retained = [gap for gap in group.gaps if gap.get("id") not in entries]
        group.gaps = [*retained, *({"id": identity, "reason": "input_budget_exceeded"} for identity in omitted)]
        return selected, duplicate, omitted

    def _fits_context(
        self,
        *,
        extra: dict[str, dict[str, object]] | None = None,
        evidence_ids: list[str] | None = None,
        receipt: dict[str, object] | None = None,
        replace_last_action: bool = True,
    ) -> bool:
        self._refresh_history_chars()
        available = remaining_material_chars(
            INTEGRATION_PROMPT,
            self._build_context(extra=extra, evidence_ids=evidence_ids, last_action=receipt if replace_last_action else None),
            self._active_tools(),
            self._effective_options(),
        )
        # 回执返回后才由循环加入实际历史, 选材时先预留其开销, 不重复登记历史计数.
        receipt_chars = len(_json(receipt)) if receipt is not None else 0
        return available > max(1, self._history_chars + receipt_chars)

    def _effective_options(self) -> AnalysisOptions:
        """直调工具也按当前阶段解析, 避免测试和运行使用不同预算."""
        return resolve_phase_options(self.options, "outline" if self.outline is None else "integration")[0]

    def _finish_analysis(self) -> str:
        self._require_open_group()
        if self.comparisons.active is not None:
            msg = f"Submit or defer active comparison first: {self.comparisons.active.id}"
            raise ValueError(msg)
        self.finish_requested = True
        self.finalize()
        return _json(self.last_action)

    def integration_tools(self) -> list[BaseTool]:
        """提供索引发现、按需读取、批量提交、测量与显式结束."""

        @tool(args_schema=IntegrationDecision)
        def submit_integration(**arguments: object) -> str:
            """提交当前比较目标的决定; preserve 保留未修改目标, 不关闭辅助材料的比较."""
            return self._submit_integration(arguments)

        @tool(args_schema=ReadLibraryInput)
        def read_library(**arguments: object) -> str:
            """按 ID 批量选择完整正文, 首读声明比较目标, 同组补读默认仅作辅助材料."""
            with self.lock:
                return self._read_library(arguments)

        @tool(args_schema=ListLibraryInput)
        def list_library_tool(**arguments: object) -> str:
            """过滤和分页发现库条目; 索引不代表正文已呈现或比较完成."""
            with self.lock:
                self._require_open_group()
                query = ListLibraryInput.model_validate(arguments)
                return self._query_receipt(
                    list_library(
                        self._require_store(),
                        library=query.library,
                        element_ids=query.element_ids,
                        endpoint_ids=query.endpoint_ids,
                        statuses=query.statuses,
                        owner_ids=query.owner_ids,
                        cursor=query.cursor,
                        limit=query.limit,
                        processing_status=self.comparisons.status_summary(self._require_store()),
                        max_chars=self.options.library_page_chars,
                    )
                )

        list_library_tool.name = "list_library"

        @tool(args_schema=ListComparisonWorkInput)
        def list_comparison_work(**arguments: object) -> str:
            """分页发现必要工作或查询指定工作; 未显示的工作仍阻止完整交付."""
            with self.lock:
                self._require_open_group()
                query = ListComparisonWorkInput.model_validate(arguments)
                page = self.comparisons.list_work(**query.model_dump(), max_chars=self.options.library_page_chars)
                return self._query_receipt(page)

        @tool
        def list_analysis_context(
            section: Literal["outline_issues", "failed_tasks", "uncovered_elements"], cursor: str | None = None, limit: int = 30
        ) -> str:
            """分页读取影响完整交付的初稿问题、失败任务或未覆盖元素.

            Args:
                section: 控制信息类别.
                cursor: 上一页返回的游标; 状态变化后需重新查询.
                limit: 本页最多返回的条目数, 范围 1 到 100.
            """
            with self.lock:
                self._require_open_group()
                if not 1 <= limit <= MAX_CONTEXT_PAGE_SIZE:
                    msg = "limit must be between 1 and 100"
                    raise ValueError(msg)
                return self._query_receipt(self._context_page(section, cursor=cursor, limit=limit))

        @tool
        def finish_analysis() -> str:
            """声明无剩余必要整合; 后端检查工作、缺口与引用并导出四库."""
            with self.lock:
                return self._finish_analysis()

        identity = f"{self.task_id}-integration-{self.integration_cycles}"
        self.submission_handler = SubmissionHandler(
            identity, submit_integration, self._submit_integration, lambda: None, self.lock, directory=self.directory
        )
        return [
            submit_integration,
            self.submission_handler.repair_tool(),
            read_library,
            list_library_tool,
            list_comparison_work,
            list_analysis_context,
            finish_analysis,
            self._stop_tool(),
            self._measurement_tool(),
            self._read_measurements_tool(),
        ]

    def _query_receipt(self, page: dict[str, object]) -> str:
        """目录虽不选正文, 返回值也必须能进入下一轮请求."""
        if not self._fits_context(receipt=page, replace_last_action=False):
            msg = "query_history_exceeded: page does not fit; reduce limit or submit/defer the current comparison"
            raise ValueError(msg)
        return _json(page)

    def _read_measurements_tool(self) -> BaseTool:
        @tool
        def read_measurements(evidence_ids: list[str]) -> str:
            """批量复用本运行已有证据, 下一请求共同提供正文及图像; 不占用新测量额度.

            Args:
                evidence_ids: 控制反馈中已登记的测量身份.
            """
            with self.lock:
                if self.group_accepted or self.stop_reason != "running":
                    msg = "Current comparison already ended"
                    raise ValueError(msg)
                if not evidence_ids or any(identity not in self.state.get("measurements", {}) for identity in evidence_ids):
                    msg = "Select existing measurement identities in this run"
                    raise ValueError(msg)
                self._require_comparison_for_measurements()
                bound: dict[str, object] = {
                    "status": "selected",
                    "evidence_ids": list(dict.fromkeys([*self.selected_evidence, *evidence_ids])),
                    "not_provided_ids": evidence_ids,
                }
                if not self._fits_context(receipt=bound, replace_last_action=False):
                    msg = "measurement_receipt_exceeded: reduce evidence_ids or submit/defer current comparison"
                    raise ValueError(msg)
                omitted = self._select_evidence(evidence_ids, receipt=bound, replace_last_action=False)
                return _json({"status": "selected", "evidence_ids": self.selected_evidence, "not_provided_ids": omitted})

        return read_measurements

    def _measurement_tool(self) -> BaseTool:
        @tool
        def measure_reference(requests: list[MeasurementRequest]) -> str:
            """批量测量固定原图, 下一请求自动提供本批所有结果与裁剪图.

            Args:
                requests: 测量问题与像素区域规格.
            """
            with self.lock:
                if self.group_accepted or self.stop_reason != "running":
                    msg = "Current comparison already ended"
                    raise ValueError(msg)
                if not requests or len(requests) > self.options.max_measurements:
                    msg = "Provide a nonempty measurement batch within the configured budget"
                    raise ValueError(msg)
                self._require_comparison_for_measurements()
                receipts = []
                for request in requests:
                    record, cached = self.measurements.measure(request)
                    self.state = {**self.state, "measurements": {**self.state.get("measurements", {}), record.id: record}}
                    receipts.append({"evidence_id": record.id, "cached": cached, "selected": False})
                bound: dict[str, object] = {"status": "measured", "results": receipts}
                if not self._fits_context(receipt=bound):
                    self.save()
                    msg = "measurement_receipt_exceeded: measurements saved; query fewer evidence_ids after submitting/deferring current comparison"
                    raise ValueError(msg)
                for receipt in receipts:
                    receipt["selected"] = not self._select_evidence([str(receipt["evidence_id"])], receipt=bound)
                self.last_action = {"status": "measured", "results": receipts}
                self.save()
                return _json(self.last_action)

        return measure_reference

    def _require_comparison_for_measurements(self) -> None:
        if self.comparisons.active is None:
            msg = "Read a comparison scope before selecting measurements"
            raise ValueError(msg)

    def _select_evidence(self, identities: list[str], *, receipt: dict[str, object] | None = None, replace_last_action: bool = True) -> list[str]:
        omitted = []
        for identity in identities:
            selection = list(dict.fromkeys([*self.selected_evidence, identity]))
            if self._fits_context(evidence_ids=selection, receipt=receipt, replace_last_action=replace_last_action):
                self.selected_evidence = selection
            else:
                omitted.append(identity)
        return omitted

    def _active_tools(self) -> list[BaseTool]:
        exhausted = len(self.state.get("measurements", {})) >= self.options.max_measurements
        return [item for item in self._tools if item.name != "measure_reference" or not exhausted]

    def _check_request(self, request: ModelRequest) -> None:
        try:
            check_request(request, self._active_tools(), self._effective_options())
        except RequestBudgetError as exc:
            if self.submission_handler and self.submission_handler.draft and self.submission_handler.draft.get("status") != "submitted":
                msg = "repair_context_exceeded: complete draft and required materials do not fit"
                raise RepairContextError(msg) from exc
            raise
        self._last_request = request
        whole = request_sizes(request, self._active_tools())
        context = request_sizes(request.override(messages=[self._build_context()]), self._active_tools())
        self._history_chars = max(0, whole["message_text_chars"] - context["message_text_chars"])
        self._history_chars += max(0, whole["image_count"] - context["image_count"]) * self.options.request_image_tokens

    def _record_history(self, messages: list[BaseMessage]) -> None:
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

    def _run_phase(self, model: BaseChatModel, *, initial: bool) -> None:
        phase = "outline" if initial else "integration"
        self.phase_options, source = resolve_phase_options(self.options, phase)
        self.phase_budgets[phase] = {"max_output_tokens": self.phase_options.max_output_tokens, "source": source}
        self.save()
        start = self.execution.model_calls
        limit = start + self.options.max_integration_calls
        if self.options.max_main_calls:
            limit = min(limit, self.options.max_main_calls)
        self._tools = self.outline_tools() if initial else self.integration_tools()
        loop = AnalysisLoop(
            self.execution,
            limit,
            self.context,
            lambda: self.stop_reason != "running" or (self.outline is not None if initial else self.group_accepted),
            self._tools,
            request_retries=self.options.max_request_retries,
            submission_handler=self.submission_handler,
            on_prepared=self.on_prepared,
            role_prompt_only=True,
            before_request=self._check_request,
            available_tools=self._active_tools,
            on_history=self._record_history,
            prepare_history=self._prepare_history,
            repair_scope=lambda: "outline" if initial else self.comparisons.active.id if self.comparisons.active else "discovery",
            repair_scope_active=lambda scope: self._repair_scope_active(scope, initial=initial),
            on_tool_result=self._save_tool_result,
            on_event=lambda event, details: self.event(
                "phase_checkpoint" if event == "task_completed" else event, {"phase": "outline" if initial else "integration", **details}
            ),
            max_repeated_no_progress=self.options.max_repeated_no_progress or self.options.integration_no_progress,
            progress=lambda: (
                self.store.revision if self.store else 0,
                len(self.state.get("measurements", {})),
                tuple(sorted(self.comparisons.active.versions.items())) if self.comparisons.active else (),
            ),
        )
        loop.run(
            configure_analysis_model(cast("ChatOpenAI", model), self.phase_options),
            OUTLINE_PROMPT if initial else INTEGRATION_PROMPT,
            self.config(self.task_id),
        )

    def _repair_scope_active(self, scope: str, *, initial: bool) -> bool:
        """阶段切换后不再修复已退出的错误, 原身份已消耗额度仍保留."""
        if initial:
            return scope == "outline" and self.outline is None
        group = self.comparisons.active
        return scope == (group.id if group else "discovery")

    def _save_tool_result(self, record: dict[str, JsonValue]) -> None:
        """完整工具交互只保存到制品, 不进入模型默认上下文."""
        payload = {"model_call": self.execution.model_calls, "work_id": self.comparisons.active.id if self.comparisons.active else None, **record}
        with self.lock, (self.directory / "tools.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(_json(payload) + "\n")

    def _integrate(self, model: BaseChatModel) -> None:
        while self.stop_reason == "running":
            if self.integration_cycles >= self.options.max_integration_packages:
                self.gaps.append({"reason": "integration_group_budget_exhausted"})
                break
            self.integration_cycles += 1
            self.group_accepted = False
            self._accepted_arguments = None
            self._history_chars = 0
            self._history_messages = []
            self._consumed_calls = set()
            if self.comparisons.active is None:
                self.selected_evidence, self.presented_evidence = [], set()
                self.submission_handler = None
                self.last_action = {"status": "select_next_comparison"}
            self.save()
            try:
                self._run_phase(model, initial=False)
            except StaleMaterialError:
                previous = self.submission_handler.draft if self.submission_handler else None
                self.comparisons.refresh(self._require_store())
                self.last_action = {"status": "materials_refreshed", "previous_decision": previous.get("arguments") if previous else None}
                self.save()
            except RepairContextError as exc:
                if self.comparisons.active is None:
                    raise
                self.execution.format_repair_resolved_through[self.comparisons.active.id] = len(self.execution.tool_feedback)
                self.comparisons.defer(str(exc))
                self.save()
            except (AnalysisLimitError, AnalysisNoProgressError) as exc:
                reason = "input_budget_exceeded" if isinstance(exc, RequestBudgetError) else str(exc)
                if self.comparisons.active is not None:
                    self.comparisons.defer(reason)
                self.gaps.append({"reason": reason})
                self.save()
                break
        self.finalize()

    def run(self, model: BaseChatModel) -> None:
        """先建立初稿和方向, 独立探索全部终止后按需整合."""
        self._run_phase(model, initial=True)
        if self.stop_reason != "running":
            self.execution.status = "stopped"
            return
        self._run_batch()
        if self._require_store().report_ids:
            self._integrate(model)
        else:
            self.stop_reason, self.execution.status = "no_reports", "failed"
            self.execution.error = "No valid exploration report was committed"

    def finalize(self) -> None:
        """后端交付合法四库, 失败任务与必要工作缺口影响真实终态."""
        if self.summary_result is not None or self.store is None or not self.store.report_ids or len(self.workers) < MIN_ANALYSIS_TASKS:
            return
        uncovered = self._uncovered()
        if uncovered:
            self.gaps.append({"reason": "uncovered_elements", "element_ids": uncovered})
            absent = {
                element.id: ["本轮没有形成覆盖此元素的候选, 需要后续独立探索"]
                for element in self.store.library.elements
                if element.id in uncovered and not element.unresolved
            }
            if absent:
                self.store.update_elements_unresolved(absent)
        missing = [key for key, value in self.workers.items() if value.status != "completed"]
        unresolved = [issue for issue in self.issues if not self._issue_resolved(str(issue["id"]))]
        unfinished = self.comparisons.snapshot()
        partial = (
            self.stop_reason != "running"
            or not self.finish_requested
            or bool(missing or unresolved or self.gaps or self.comparisons.pending or self.comparisons.deferred or self.comparisons.active)
        )
        status = "partial" if partial else "completed"
        result = ResultRecord(
            id=self.directory.name + "-library",
            task_id=self.task_id,
            status=status,
            summary="可能性库已交付; 候选仍需编码与渲染验证",
            analysis_detail=self.store.library,
            analysis_protocol=PROTOCOL,
        )
        self.state = add_result(self.state, result)
        self.summary_result = result
        if self.stop_reason == "running":
            self.stop_reason = status
            self.execution.status = "completed"
        self.last_action = {"status": status, "missing_tasks": missing, "unresolved_issues": unresolved, "integration": unfinished}
        self.save()

    def _issue_resolved(self, identity: str) -> bool:
        resolution = self.resolutions.get(identity, {})
        return resolution.get("disposition") == "dismissed" or (
            resolution.get("disposition") == "revised" and resolution.get("verified_outline_version") == len(self.outline_versions)
        )

    def _execution_snapshot(self) -> dict[str, object]:
        return {"main_execution": asdict(self.execution)}

    def save(self) -> None:
        """保存派生运行记录; 库版本与批量回执以库快照为权威."""
        options = {**asdict(self.options), "output_dir": str(self.options.output_dir) if self.options.output_dir else None}
        save_run(
            self.directory,
            self.state,
            {
                "kind": "independent_exploration",
                "analysis_protocol": PROTOCOL,
                "task_id": self.task_id,
                "options": options,
                "phase_budgets": self.phase_budgets,
                "stop_reason": self.stop_reason,
                **self._execution_snapshot(),
                "worker_executions": {key: asdict(value) for key, value in self.workers.items()},
                "visual_outline": compact_data(self.outline) if self.outline else None,
                "outline_versions": [compact_data(value) for value in self.outline_versions],
                "issues": self.issues,
                "issue_resolutions": self.resolutions,
                "gaps": self.gaps,
                "integration": self.comparisons.snapshot(),
                "summary_result_id": self.summary_result.id if self.summary_result else None,
                "reference_sha256": self.measurements.image_sha256,
                "presented_library_items": sorted(self.store.presented) if self.store else [],
            },
        )

    def outcome(self) -> AnalysisOutcome:
        """返回真实完成或部分交付状态, 保持公开入口包装类型."""
        return AnalysisOutcome(
            state=self.state, task_id=self.task_id, summary_result=self.summary_result, run_dir=self.directory, stop_reason=self.stop_reason
        )
