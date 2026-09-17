"""并行执行独立视角分析, 串行提交黑板状态与运行快照."""

from __future__ import annotations

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict, fields, is_dataclass, replace
from threading import Lock
from typing import TYPE_CHECKING, Annotated, cast

from langchain.messages import HumanMessage
from langchain.tools import tool
from langchain_core.tools import InjectedToolCallId

from shader_deep.analysis.events import EventLog
from shader_deep.analysis.evidence import MeasurementRequest  # noqa: TC001  # 工具参数结构需要在运行时解析.
from shader_deep.analysis.measurements import ReferenceMeasurements
from shader_deep.analysis.presets import PRESET_LENSES
from shader_deep.analysis.references import unlinked_interpretations
from shader_deep.analysis.report_files import AnalysisReportFiles
from shader_deep.analysis.schemas import AnalysisSummary, AnalysisTaskRequest, LensConfig, SourceRef, VisualDecomposition
from shader_deep.analysis.submissions import SubmissionHandler
from shader_deep.analysis.types import MIN_ANALYSIS_TASKS, AnalysisExecution, AnalysisOutcome, ToolFeedback
from shader_deep.analysis.validation import (
    AnalysisValidationError,
    validate_summary_visual,
    validate_task_visual,
    validate_visual_decomposition,
    validate_visual_evidence,
)
from shader_deep.analysis.worker import run_lens
from shader_deep.artifacts import save_run
from shader_deep.blackboard import add_result, add_task
from shader_deep.context.analysis import build_analysis_context, lens_result_ids
from shader_deep.schemas import ResultRecord, TaskRecord

if TYPE_CHECKING:
    from pathlib import Path

    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import RunnableConfig

    from shader_deep.analysis.schemas import VisualMapping
    from shader_deep.analysis.submissions import SubmissionReply
    from shader_deep.analysis.types import AnalysisOptions
    from shader_deep.schemas import BlackboardState


class AnalysisSession:
    """维护一次独立分析会话; 工作线程返回新增结果, 由协调器合并到最新黑板.

    一轮流程为: 规划视角、并行执行、读取报告、按需测量或追加分析、提交综合结果.
    子任务只使用派发时的快照; 协调器逐条合并结果, 避免旧快照覆盖其他线程的产出.
    工具入口共用一把锁, 覆盖整批派发与提交, 但批次内部的视角分析仍并发执行.
    """

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """绑定新的主分析任务、固定参考图与执行预算.

        Args:
            state: 初始黑板状态.
            task_id: 主分析任务标识.
            options: 由程序控制的执行预算.
            directory: 保存参考图快照的独立运行目录.
            reference_url: 由固定参考图字节编码的数据 URL.
        """
        self.state: BlackboardState = state
        self.task_id, self.options = task_id, options
        self.directory, self.reference_url = directory, reference_url
        self.execution = AnalysisExecution()
        self.workers: dict[str, AnalysisExecution] = {}
        self.report_files = AnalysisReportFiles(directory)
        # 报告目录不授予引用资格; 追加任务至少需要读到所选报告中的正文.
        self.presented = self.report_files.with_body
        self.summary_result: ResultRecord | None = None
        self.stop_reason = "running"
        self.lock = Lock()
        self.measurements = ReferenceMeasurements(
            base64.b64decode(reference_url.split(",", 1)[1]),
            directory,
            task_id,
            state["tasks"][task_id].target_version,
            options.max_measurements,
        )
        self.presented_evidence: set[str] = set()
        self.visual_decomposition: VisualDecomposition | None = None
        self.initial_visual_decomposition: VisualDecomposition | None = None
        self.task_visual_inputs: dict[str, VisualDecomposition | None] = {}
        self.presented_visual_inputs: set[str] = set()
        self._pending_visual_context: tuple[dict[str, object], set[str]] | None = None
        self.task_requests: dict[str, AnalysisTaskRequest] = {}
        self.events = EventLog(directory)
        self.event = self.events.bind(task_id, "coordinator", self.execution)
        self.submission_handler: SubmissionHandler | None = None
        self._tools: list[BaseTool] | None = None

    def config(self, task_id: str) -> RunnableConfig:
        """为嵌套模型与工具调用设置任务归属和追踪信息.

        Args:
            task_id: 即将执行的主分析任务或视角任务标识.

        Returns:
            框架执行配置及本次运行共享的会话标识.
        """
        task = self.state["tasks"][task_id]
        limit = self.options.max_main_calls if task.lens_config is None else self.options.max_worker_calls
        return {
            "run_name": "shader-deep.analysis" if task.lens_config is None else "shader-deep.analysis.lens",
            "tags": ["analysis", "coordinator" if task.lens_config is None else task.lens_config.id],
            "metadata": {
                "task_id": task_id,
                "parent_task_id": task.parent_task_id,
                "target_version": task.target_version,
                "session_id": self.directory.name,
                "run_dir": str(self.directory),
            },
            # LangGraph 要求正整数; 不限次数时使用平台最大整数, 避免默认图步数先截断.
            "recursion_limit": sys.maxsize if limit == 0 else 4 * limit + 10,
        }

    def context(self) -> HumanMessage:
        """刷新主分析 Agent 可见的报告、证据与剩余预算.

        Returns:
            包含参考图实际字节的当前任务上下文.
        """
        for identifier in lens_result_ids(self.state, self.task_id):
            self.report_files.register(self.state["results"][identifier])
        message = build_analysis_context(
            self.state,
            self.task_id,
            self.reference_url,
            limits={
                "tasks_remaining": self.options.max_tasks - len(self.workers),
                "model_calls_remaining": self.options.max_main_calls - self.execution.model_calls if self.options.max_main_calls else None,
                "max_parallel": self.options.max_parallel,
                "max_worker_calls": self.options.max_worker_calls,
                "measurements_remaining": self.options.max_measurements - len(self.measurements.records),
                "measurements_total": self.options.max_measurements,
                "measurements_used": len(self.measurements.records),
                "phase": "review" if self.workers else "initial_observation",
                "allowed_purpose": "supplement or verify; gap and expected_evidence required" if self.workers else "initial; no prior reports",
            },
            image_size=self.measurements.image_size,
            visual_decomposition=self.visual_decomposition,
            initial_visual_decomposition=self.initial_visual_decomposition,
            task_visual_inputs=self.task_visual_inputs,
            submission=self.submission_handler.snapshot() if self.submission_handler is not None else None,
        )
        # 记录材料已加入模型请求, 不证明模型已正确阅读或理解材料.
        self.presented_evidence.update(item.id for item in self.measurements.records.values())
        self._pending_visual_context = (cast("dict[str, object]", message.content[0]), set(self.task_visual_inputs))
        return message

    def on_prepared(self, messages: list[BaseMessage]) -> None:
        """读取工具正文进入实际下一轮请求后, 才允许引用其中的对象."""
        count = self.report_files.on_prepared(messages)
        if count:
            self.event("materials_presented", {"new_pointers": count})
        if self._pending_visual_context is not None:
            block, task_ids = self._pending_visual_context
            if any(isinstance(message, HumanMessage) and isinstance(message.content, list) and block in message.content for message in messages):
                self.presented_visual_inputs.update(task_ids)
                self._pending_visual_context = None

    def progress(self) -> tuple[int, int, int]:
        """仅计算新业务材料, 重复读取或重复提交不伪装成进展."""
        return len(self.measurements.records), self.report_files.body_count(), len(self.state["results"])

    def save(self) -> None:
        """持久化已提交结果、计数和错误, 不写入模型配置凭据."""
        options = {**asdict(self.options), "output_dir": str(self.options.output_dir) if self.options.output_dir is not None else None}
        save_run(
            self.directory,
            self.state,
            {
                "kind": "multi_view_analysis",
                "task_id": self.task_id,
                "options": options,
                "reference_snapshot": str(self.directory / "reference.png"),
                "stop_reason": self.stop_reason,
                "main_execution": asdict(self.execution),
                "worker_executions": {key: asdict(value) for key, value in self.workers.items()},
                "summary_result_id": self.summary_result.id if self.summary_result else None,
                "measurement_calls": self.measurements.calls,
                "presented_report_pointers": {key: sorted(value) for key, value in self.report_files.presented.items()},
                "submission": self.submission_handler.snapshot() if self.submission_handler is not None else None,
                "unlinked_interpretations": (
                    unlinked_interpretations(self.state, self.summary_result.analysis_detail)
                    if self.summary_result is not None and isinstance(self.summary_result.analysis_detail, AnalysisSummary)
                    else []
                ),
                "reference_sha256": self.measurements.image_sha256,
                "visual_decomposition": asdict(self.visual_decomposition) if self.visual_decomposition is not None else None,
                "initial_visual_decomposition": asdict(self.initial_visual_decomposition) if self.initial_visual_decomposition is not None else None,
                "task_inputs": {
                    identifier: {
                        "request": asdict(self.task_requests[identifier]),
                        "visual_decomposition": asdict(visual) if visual is not None else None,
                    }
                    for identifier, visual in self.task_visual_inputs.items()
                },
            },
        )

    def _lens(self, request: AnalysisTaskRequest, identifier: str) -> LensConfig:
        """解析预设或冻结自定义视角, 由程序分配自定义视角的 ID 与来源."""
        if request.preset_id is not None:
            for lens in PRESET_LENSES:
                if lens.id == request.preset_id:
                    return lens
            msg = f"Unknown preset: {request.preset_id}"
            raise ValueError(msg)
        if request.lens is None:
            msg = "Missing perspective configuration"
            raise ValueError(msg)
        return LensConfig(id=f"{identifier}-lens", origin="generated", **asdict(request.lens))

    def _register(self, requests: list[AnalysisTaskRequest], *, visual_decomposition: VisualDecomposition | None = None) -> list[TaskRecord]:
        """先在局部状态中校验整批任务, 再一次性发布到当前会话."""
        if not requests or len(self.workers) + len(requests) > self.options.max_tasks:
            msg = "Empty batch or analysis task budget exceeded"
            raise ValueError(msg)
        self._validate_batch_plan(requests)
        visual = visual_decomposition if visual_decomposition is not None else self.visual_decomposition
        if visual is not None:
            validate_visual_decomposition(visual, image_size=self.measurements.image_size)
            validate_visual_evidence(self.state, self.state["tasks"][self.task_id], visual)
            self._validate_visual_inputs(requests, visual)
        # add_task 返回新字典, 因此中途有请求不合法时, self.state 仍保留原来的完整状态.
        # 编号按已登记任务数递增; 失败任务也保留编号并占用总任务预算.
        state, tasks = self.state, []
        for index, request in enumerate(requests, start=len(self.workers) + 1):
            request_path = f"/requests/{index - len(self.workers) - 1}"
            validate_task_visual(request, visual, path=request_path)
            identifier = f"{self.directory.name}-a{index:03d}"
            task = TaskRecord(
                id=identifier,
                role="analysis",
                target_version=state["tasks"][self.task_id].target_version,
                objective=request.objective,
                parent_task_id=self.task_id,
                lens_config=self._lens(request, identifier),
                related_result_ids=request.related_result_ids,
                analysis_purpose=request.purpose,
                analysis_gap=request.gap,
                expected_evidence=request.expected_evidence,
                evidence_ids=request.evidence_ids,
            )
            try:
                state = add_task(state, task)
            except AnalysisValidationError as exc:
                raise AnalysisValidationError([{**issue, "path": request_path + str(issue["path"])} for issue in exc.issues]) from exc
            if visual is not None:
                # 子任务收到的完整初稿若引用证据, 该证据必须同样明确选入并回读.
                validate_visual_evidence(state, task, visual)
            tasks.append(task)
        # 来源合法性全部通过后再检查实际呈现, 不把未知或越界来源误报为未读.
        issues = self._batch_presented_issues(requests) + self._source_issues(visual, "/visual_decomposition")
        if issues:
            raise AnalysisValidationError(issues)
        self.state = state  # 整批任务全部校验通过后才提交.
        self.visual_decomposition = visual
        if self.initial_visual_decomposition is None:
            self.initial_visual_decomposition = visual
        self.task_visual_inputs.update({task.id: visual for task in tasks})
        self.task_requests.update({task.id: request for task, request in zip(tasks, requests, strict=True)})
        self.workers.update({task.id: AnalysisExecution() for task in tasks})
        self.save()
        return tasks

    def _validate_visual_inputs(self, requests: list[AnalysisTaskRequest], visual: VisualDecomposition) -> None:
        """一次反馈各请求缺少的全部初稿依赖, 保留显式选材而不自动补入."""
        items = (*visual.elements, *visual.features, *visual.relations)
        evidence = {identifier for item in items for identifier in item.evidence_ids}
        sources = {reference.result_id for item in items for reference in item.source_refs}
        issues: list[dict[str, object]] = []
        for index, request in enumerate(requests):
            for field, required in (("evidence_ids", evidence), ("related_result_ids", sources)):
                if missing := sorted(required - set(getattr(request, field))):
                    issues.append(
                        {
                            "path": f"/requests/{index}/{field}",
                            "code": "missing_visual_inputs",
                            "message": f"Visual draft inputs were not provided to every task; explicitly select the missing IDs: {missing}",
                        }
                    )
        if issues:
            raise AnalysisValidationError(issues)

    def _validate_batch_plan(self, requests: list[AnalysisTaskRequest]) -> None:
        """区分首轮独立分析与后续补充、复核, 来源检查随后在局部任务上执行."""
        if not self.workers and len(requests) < MIN_ANALYSIS_TASKS:
            msg = "The initial batch needs at least two complementary perspectives"
            raise ValueError(msg)
        for request in requests:
            if not self.workers and (request.purpose != "initial" or request.related_result_ids):
                msg = "Initial perspectives require purpose initial and no prior reports"
                raise ValueError(msg)
            if self.workers and (request.purpose == "initial" or not request.gap or not request.expected_evidence):
                msg = "Follow-ups require purpose supplement/verify, gap (why it matters), and expected_evidence"
                raise ValueError(msg)
        # 黑板的基础输入检查会先于分析校验运行, 在此保留同样前置并补工具参数路径.
        issues: list[dict[str, object]] = [
            {
                "path": f"/requests/{index}/related_result_ids/{offset}",
                "code": "source_not_found",
                "message": f"Unknown result: {identifier}",
            }
            for index, request in enumerate(requests)
            for offset, identifier in enumerate(request.related_result_ids)
            if identifier not in self.state["results"]
        ]
        if issues:
            raise AnalysisValidationError(issues)

    def _batch_presented_issues(self, requests: list[AnalysisTaskRequest]) -> list[dict[str, object]]:
        """派发的报告至少有一项正文, 测量已呈现; 按实际输入位置聚合未读项."""
        issues = self._source_issues(requests, "/requests")
        for index, request in enumerate(requests):
            issues.extend(
                {
                    "path": f"/requests/{index}/related_result_ids/{offset}",
                    "code": "source_not_presented",
                    "message": f"Receive source reports before dispatching a task that uses them: {identifier}",
                }
                for offset, identifier in enumerate(request.related_result_ids)
                if identifier not in self.presented
            )
        return issues

    def _evidence_presented_issues(self, identifiers: tuple[str, ...], path: str) -> list[dict[str, object]]:
        """测量身份已验证后, 收集尚未实际呈现的证据位置."""
        return [
            {
                "path": f"{path}/{index}",
                "code": "evidence_not_presented",
                "message": f"Receive measurement results in context before citing or dispatching them: {identifier}",
            }
            for index, identifier in enumerate(identifiers)
            if identifier not in self.presented_evidence
        ]

    def _source_issues(self, value: object, path: str) -> list[dict[str, object]]:
        """递归收集报告与测量的未读位置, 包括视觉事实与假设继承关系."""
        if isinstance(value, SourceRef):
            if self.report_files.has_source(value):
                return []
            return [
                {
                    "path": path,
                    "code": "source_not_presented",
                    "message": f"Read source body in a model request before citing: {value.result_id}/{value.item_id}",
                    "candidates": [
                        item for item in self.report_files.catalog() if item["result_id"] == value.result_id and item["item_id"] == value.item_id
                    ],
                }
            ]
        if is_dataclass(value) and not isinstance(value, type):
            issues = []
            for field in fields(value):
                location, item = f"{path}/{field.name}", getattr(value, field.name)
                issues.extend(
                    self._evidence_presented_issues(item, location) if field.name == "evidence_ids" else self._source_issues(item, location)
                )
            return issues
        if isinstance(value, (list, tuple)):
            return [issue for index, item in enumerate(value) for issue in self._source_issues(item, f"{path}/{index}")]
        return []

    def _mapping_presented_issues(self, mapping: VisualMapping, path: str) -> list[dict[str, object]]:
        """报告绑定初稿按已注入快照检查, 报告新增对象仍需实际读取正文."""
        if mapping.source_result_id is None:
            return []
        task_id = self.state["results"][mapping.source_result_id].task_id
        visual = self.task_visual_inputs.get(task_id)
        items = {"element": visual.elements, "feature": visual.features, "relation": visual.relations}[mapping.kind] if visual else ()
        if any(item.id == mapping.source_id for item in items):
            if task_id not in self.presented_visual_inputs:
                return [
                    {
                        "path": f"{path}/source_id",
                        "code": "source_not_presented",
                        "message": "Receive the source task's visual snapshot before mapping its objects",
                    }
                ]
            return []
        return self._source_issues(SourceRef(result_id=mapping.source_result_id, item_id=mapping.source_id), f"{path}/source_id")

    def _run_batch(self, requests: list[AnalysisTaskRequest], *, visual_decomposition: VisualDecomposition | None = None) -> str:
        """并发执行已登记任务, 按完成顺序合并结果并逐次保存快照."""
        tasks = self._register(requests, visual_decomposition=visual_decomposition)
        with ThreadPoolExecutor(max_workers=self.options.max_parallel, thread_name_prefix="analysis-lens") as executor:
            # 每个任务单独复制 contextvars, 继承当前追踪链路.
            # 同一个 Context 不能同时进入多个线程, 因此不能在循环外只复制一次.
            futures = {
                executor.submit(
                    copy_context().run,
                    run_lens,
                    self.state,
                    task.id,
                    self.reference_url,
                    self.options.max_worker_calls,
                    self.config(task.id),
                    options=self.options,
                    image_size=self.measurements.image_size,
                    visual_decomposition=self.task_visual_inputs[task.id],
                    focus_element_ids=self.task_requests[task.id].focus_element_ids,
                    focus_feature_ids=self.task_requests[task.id].focus_feature_ids,
                    directory=self.directory,
                    event_log=self.events,
                ): task.id
                for task in tasks
            }
            for future in as_completed(futures):
                # Context.run 保留追踪上下文, 但静态检查无法推导原函数的返回类型.
                result, execution = cast("tuple[ResultRecord, AnalysisExecution]", future.result())
                # 始终基于最新状态登记单条结果; 工作线程不返回整份黑板供覆盖.
                self.state = add_result(self.state, result)
                if result.analysis_detail is not None:
                    self.report_files.register(result)
                self.workers[result.task_id] = execution
                self.event("report_registered", {"result_id": result.id, "status": result.status})
                self.save()
        results = [result for result in self.state["results"].values() if result.task_id in {task.id for task in tasks}]
        # 正文通过只读文件工具按需取得; 下一轮只提供目录与来源定位.
        receipts = [{"id": result.id, "task_id": result.task_id, "status": result.status, "summary": result.summary[:240]} for result in results]
        return json.dumps(
            {
                "status": "batch_completed",
                "results": receipts,
                "next": "Use read_analysis_file with the next context's report_files and source_catalog",
            },
            ensure_ascii=False,
        )

    def _measure(self, requests: list[MeasurementRequest]) -> str:
        """处理视觉拆分与批次之间的测量, 全程共用预算与缓存."""
        if not requests or len(requests) > self.options.max_measurements:
            msg = "Measurement batch must be nonempty and fit the configured batch limit"
            raise ValueError(msg)
        responses = []
        for request in requests:
            try:
                result, cached = self.measurements.measure(request)
                self.state = {**self.state, "measurements": {**self.state.get("measurements", {}), result.id: result}}
                responses.append({"status": "cached" if cached else "measured", "evidence_id": result.id, "kind": result.spec.kind})
                self.event("measurement_cached" if cached else "measurement_created", {"evidence_id": result.id})
                self.save()
            except ValueError as exc:
                self._record_rejection("measure_reference", str(exc))
                responses.append({"status": "invalid_measurement", "question": request.question, "message": str(exc)})
        return json.dumps(
            {"results": responses, "measurements_remaining": self.options.max_measurements - len(self.measurements.records)}, ensure_ascii=False
        )

    def _finish(self, summary: AnalysisSummary, text: str) -> str:
        """校验综合结果覆盖范围, 补齐未完成任务并保存终态."""
        available = set(lens_result_ids(self.state, self.task_id))
        if len(self.workers) < MIN_ANALYSIS_TASKS or not available:
            msg = "Run at least two lens tasks before finishing"
            raise ValueError(msg)
        if set(summary.source_result_ids) != available or not text.strip():
            msg = "Summary must cover all usable reports and include a nonblank summary text"
            raise ValueError(msg)
        validate_summary_visual(
            self.state,
            summary,
            decomposition=self.initial_visual_decomposition,
            source_decompositions={identifier: self.task_visual_inputs[self.state["results"][identifier].task_id] for identifier in available},
            image_size=self.measurements.image_size,
        )
        # 缺失任务由执行状态计算, 不能让模型自行省略失败视角或把运行改报为全部成功.
        # 可用报告全部纳入综合后, 仍有失败或停止任务时, 整体只能标记为 partial.
        missing = tuple(task_id for task_id, execution in self.workers.items() if execution.status != "completed")
        summary = replace(summary, missing_task_ids=missing)
        result = ResultRecord(
            id=f"{self.directory.name}-summary",
            task_id=self.task_id,
            status="partial" if missing else "completed",
            summary=text,
            analysis_detail=summary,
        )
        # 先核对来源身份与类型, 再检查实际回读; 未读不能掩盖不存在的来源或错误类型.
        # add_result 返回局部新状态, 所有检查通过前都不发布到会话.
        staged_state = add_result(self.state, result)
        issues = self._source_issues(summary, "/summary")
        for index, mapping in enumerate(summary.visual_mappings):
            issues.extend(self._mapping_presented_issues(mapping, f"/summary/visual_mappings/{index}"))
        if issues:
            raise AnalysisValidationError(issues)
        self.state = staged_state
        self.summary_result = result
        self.stop_reason = "partial" if missing else "completed"
        unlinked = unlinked_interpretations(self.state, summary)
        self.event("analysis_submitted", {"result_id": result.id, "unlinked_interpretations": len(unlinked)})
        self.save()
        return json.dumps(
            {
                "status": self.stop_reason,
                "result_id": result.id,
                "unlinked_interpretations": len(unlinked),
                "unlinked_list": "run.json#/unlinked_interpretations",
            }
        )

    def _record_rejection(self, tool_name: str, message: str) -> None:
        """保留业务拒绝轮次和原因, 包括一批测量中的部分失败."""
        self.execution.tool_feedback += (
            ToolFeedback(model_call=self.execution.model_calls, tool=tool_name, message=message[:1400], category="business_rejection"),
        )
        self.save()

    def tools(self) -> list[BaseTool]:
        """绑定主分析 Agent 的批次、测量和结束工具, 串行提交状态.

        Returns:
            主分析 Agent 唯一允许调用的工具列表.
        """
        if self._tools is not None:
            return self._tools

        @tool
        def run_analysis_batch(requests: list[AnalysisTaskRequest], *, visual_decomposition: VisualDecomposition | None = None) -> str:
            """执行一批独立视角任务, 每个任务通过 preset_id 或新建 lens 指定视角.

            Args:
                requests: 非空任务列表; 追加分析必须显式指定要引用的历史结果 ID.
                visual_decomposition: 可选的完整视觉拆分初稿或修订; 当前批次固定绑定此快照, 省略时沿用上次版本.
            """
            with self.lock:
                if self.summary_result is not None:
                    return json.dumps({"status": "already_finished"})
                try:
                    return self._run_batch(requests, visual_decomposition=visual_decomposition)
                except (KeyError, TypeError, ValueError) as exc:
                    self._record_rejection("run_analysis_batch", str(exc))
                    return json.dumps({"status": "invalid_batch", "message": str(exc)}, ensure_ascii=False)

        @tool
        def measure_reference(requests: list[MeasurementRequest]) -> str:
            """在视觉拆分或分析批次之间测量固定参考图, 相同操作共用预算并复用证据.

            Args:
                requests: 测量目的及原图像素坐标下的 crop、region_stats 或 line_profile 规格.
            """
            with self.lock:
                if self.summary_result is not None:
                    return json.dumps({"status": "already_finished"})
                try:
                    return self._measure(requests)
                except (OSError, TypeError, ValueError) as exc:
                    self._record_rejection("measure_reference", str(exc))
                    return json.dumps({"status": "invalid_measurement", "message": str(exc)}, ensure_ascii=False)

        @tool
        def finish_analysis(summary: AnalysisSummary, text: str) -> str:
            """收到独立视角报告后, 提交保留证据来源的综合分析.

            Args:
                summary: 结构化综合结果, source_result_ids 必须覆盖所有可用报告.
                text: 简明的整体结论, 同时保留重要的不确定性.
            """
            handler = cast("SubmissionHandler", self.submission_handler)
            reply = handler.handle("finish_analysis", {"summary": asdict(summary), "text": text})
            return cast("SubmissionReply", reply).content

        self.submission_handler = SubmissionHandler(
            self.task_id,
            finish_analysis,
            lambda arguments: self._finish(cast("AnalysisSummary", arguments["summary"]), cast("str", arguments["text"])),
            lambda: json.dumps({"status": "already_finished", "result_id": self.summary_result.id}) if self.summary_result is not None else None,
            self.lock,
            directory=self.directory,
        )

        self._tools = [run_analysis_batch, measure_reference, finish_analysis, self.submission_handler.repair_tool(), self._read_tool()]
        return self._tools

    def _read_tool(self) -> BaseTool:
        """只有主会话获得文件读取入口, 子任务继续使用冻结输入."""

        @tool
        def read_analysis_file(file_path: str, pointer: str = "", *, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
            """按目录读取报告的完整 JSON 章节或条目, 下一轮收到正文后才允许引用.

            Args:
                file_path: report_files 或 source_catalog 中的虚拟文件路径.
                pointer: source_catalog 中的 JSON Pointer; 空字符串读取整报告, 过大时改为章节或条目.
                tool_call_id: 框架注入的调用标识, 不由模型填写.
            """
            with self.lock:
                try:
                    return self.report_files.read(file_path, pointer, tool_call_id)
                except (OSError, UnicodeError, TypeError, ValueError) as exc:
                    self._record_rejection("read_analysis_file", str(exc))
                    return json.dumps({"status": "invalid_read", "complete": False, "message": str(exc)}, ensure_ascii=False)

        return read_analysis_file

    def outcome(self) -> AnalysisOutcome:
        """返回已保存的综合结果及所有已提交业务记录.

        Returns:
            分析运行结果, 也可表示部分完成或未完成.
        """
        return AnalysisOutcome(
            state=self.state, task_id=self.task_id, summary_result=self.summary_result, run_dir=self.directory, stop_reason=self.stop_reason
        )
