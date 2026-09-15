"""并行执行独立视角分析, 串行提交黑板状态与运行快照."""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict, replace
from threading import Lock
from typing import TYPE_CHECKING, cast

from langchain.tools import tool

from shader_deep.analysis.evidence import MeasurementRequest  # noqa: TC001  # 工具参数结构需要在运行时解析.
from shader_deep.analysis.measurements import ReferenceMeasurements
from shader_deep.analysis.presets import PRESET_LENSES
from shader_deep.analysis.schemas import AnalysisSummary, AnalysisTaskRequest, LensConfig
from shader_deep.analysis.types import MIN_ANALYSIS_TASKS, AnalysisExecution, AnalysisOutcome
from shader_deep.analysis.worker import run_lens
from shader_deep.artifacts import save_run
from shader_deep.blackboard import add_result, add_task
from shader_deep.context.analysis import build_analysis_context, lens_result_ids
from shader_deep.schemas import ResultRecord, TaskRecord

if TYPE_CHECKING:
    from pathlib import Path

    from langchain.messages import HumanMessage
    from langchain.tools import BaseTool
    from langchain_core.runnables import RunnableConfig

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
        # presented 管报告, presented_evidence 管程序测量; 登记成功与进入请求分别记录.
        # 结束工具使用这两组资格检查, 防止在同一轮工具调用中跳过材料回读.
        self.presented: set[str] = set()
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

    def config(self, task_id: str) -> RunnableConfig:
        """为嵌套模型与工具调用设置任务归属和追踪信息.

        Args:
            task_id: 即将执行的主分析任务或视角任务标识.

        Returns:
            框架执行配置及本次运行共享的会话标识.
        """
        task = self.state["tasks"][task_id]
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
            "recursion_limit": 4 * max(self.options.max_main_calls, self.options.max_worker_calls) + 10,
        }

    def context(self) -> HumanMessage:
        """刷新主分析 Agent 可见的报告、证据与剩余预算.

        Returns:
            包含参考图实际字节的当前任务上下文.
        """
        message = build_analysis_context(
            self.state,
            self.task_id,
            self.reference_url,
            limits={
                "tasks_remaining": self.options.max_tasks - len(self.workers),
                "model_calls_remaining": self.options.max_main_calls - self.execution.model_calls,
                "max_parallel": self.options.max_parallel,
                "max_worker_calls": self.options.max_worker_calls,
                "measurements_remaining": self.options.max_measurements - len(self.measurements.records),
            },
            image_size=self.measurements.image_size,
        )
        # 记录材料已加入模型请求, 不证明模型已正确阅读或理解材料.
        self.presented.update(lens_result_ids(self.state, self.task_id))
        self.presented_evidence.update(item.id for item in self.measurements.records.values())
        return message

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
                "reference_sha256": self.measurements.image_sha256,
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

    def _register(self, requests: list[AnalysisTaskRequest]) -> list[TaskRecord]:
        """先在局部状态中校验整批任务, 再一次性发布到当前会话."""
        if not requests or len(self.workers) + len(requests) > self.options.max_tasks:
            msg = "Empty batch or analysis task budget exceeded"
            raise ValueError(msg)
        self._validate_batch_plan(requests)
        # add_task 返回新字典, 因此中途有请求不合法时, self.state 仍保留原来的完整状态.
        # 编号按已登记任务数递增; 失败任务也保留编号并占用总任务预算.
        state, tasks = self.state, []
        for index, request in enumerate(requests, start=len(self.workers) + 1):
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
            state = add_task(state, task)
            tasks.append(task)
        self.state = state  # 整批任务全部校验通过后才提交.
        self.workers.update({task.id: AnalysisExecution() for task in tasks})
        self.save()
        return tasks

    def _validate_batch_plan(self, requests: list[AnalysisTaskRequest]) -> None:
        """区分首轮独立分析与后续补充、复核, 并检查证据已进入主模型请求."""
        if not self.workers and len(requests) < MIN_ANALYSIS_TASKS:
            msg = "The initial batch needs at least two complementary perspectives"
            raise ValueError(msg)
        for request in requests:
            if not self.workers and (request.purpose != "initial" or request.related_result_ids or request.evidence_ids):
                msg = "Initial perspectives must be independent, without prior reports or measurements"
                raise ValueError(msg)
            if self.workers and (request.purpose == "initial" or not request.gap or not request.expected_evidence):
                msg = "Follow-ups require purpose supplement/verify, gap (why it matters), and expected_evidence"
                raise ValueError(msg)
            if not set(request.evidence_ids) <= self.presented_evidence:
                msg = "Receive measurement results before dispatching a follow-up that uses them"
                raise ValueError(msg)

    def _run_batch(self, requests: list[AnalysisTaskRequest]) -> str:
        """并发执行已登记任务, 按完成顺序合并结果并逐次保存快照."""
        tasks = self._register(requests)
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
                ): task.id
                for task in tasks
            }
            for future in as_completed(futures):
                # Context.run 保留追踪上下文, 但静态检查无法推导原函数的返回类型.
                result, execution = cast("tuple[ResultRecord, AnalysisExecution]", future.result())
                # 始终基于最新状态登记单条结果; 工作线程不返回整份黑板供覆盖.
                self.state = add_result(self.state, result)
                self.workers[result.task_id] = execution
                self.save()
        results = [result for result in self.state["results"].values() if result.task_id in {task.id for task in tasks}]
        # 完整报告由下一次任务上下文提供, 工具历史仅保留简短回执,
        # 避免每轮模型调用都重复携带观察条目与测量剖面.
        receipts = [{"id": result.id, "task_id": result.task_id, "status": result.status, "summary": result.summary[:240]} for result in results]
        return json.dumps({"status": "batch_completed", "results": receipts, "next": "Full reports are in the next task context"}, ensure_ascii=False)

    def _measure(self, requests: list[MeasurementRequest]) -> str:
        """处理批次之间的测量请求, 保留每项成功结果并单独反馈参数错误."""
        if not self.workers or not set(lens_result_ids(self.state, self.task_id)) <= self.presented:
            msg = "Receive the initial batch reports before measuring between batches"
            raise ValueError(msg)
        if not requests or len(requests) > self.options.max_measurements:
            msg = "Measurement batch must be nonempty and fit the configured batch limit"
            raise ValueError(msg)
        responses = []
        for request in requests:
            try:
                result, cached = self.measurements.measure(request)
                self.state = {**self.state, "measurements": {**self.state.get("measurements", {}), result.id: result}}
                responses.append({"status": "cached" if cached else "measured", "evidence_id": result.id, "kind": result.spec.kind})
                self.save()
            except ValueError as exc:
                responses.append({"status": "invalid_measurement", "question": request.question, "message": str(exc)})
        return json.dumps({"results": responses}, ensure_ascii=False)

    def _finish(self, summary: AnalysisSummary, text: str) -> str:
        """校验综合结果覆盖范围, 补齐未完成任务并保存终态."""
        available = set(lens_result_ids(self.state, self.task_id))
        if len(self.workers) < MIN_ANALYSIS_TASKS or not available or not available <= self.presented:
            msg = "Run at least two lens tasks and receive their reports in a model request before finishing"
            raise ValueError(msg)
        if set(summary.source_result_ids) != available or not text.strip():
            msg = "Summary must cover all usable reports and include a nonblank summary text"
            raise ValueError(msg)
        statements = (
            *summary.key_observations,
            *summary.relationships,
            *summary.hypotheses,
            *summary.disagreements,
            *summary.open_questions,
            *summary.implementation_hints,
        )
        if any(not set(item.evidence_ids) <= self.presented_evidence for item in statements):
            msg = "Receive measurement results in context before citing them in the summary"
            raise ValueError(msg)
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
        self.state = add_result(self.state, result)
        self.summary_result = result
        self.stop_reason = "partial" if missing else "completed"
        self.save()
        return json.dumps({"status": self.stop_reason, "result_id": result.id})

    def tools(self) -> list[BaseTool]:
        """绑定主分析 Agent 的批次、测量和结束工具, 串行提交状态.

        Returns:
            主分析 Agent 唯一允许调用的工具列表.
        """

        @tool
        def run_analysis_batch(requests: list[AnalysisTaskRequest]) -> str:
            """执行一批独立视角任务, 每个任务通过 preset_id 或新建 lens 指定视角.

            Args:
                requests: 非空任务列表; 追加分析必须显式指定要引用的历史结果 ID.
            """
            with self.lock:
                if self.summary_result is not None:
                    return json.dumps({"status": "already_finished"})
                try:
                    return self._run_batch(requests)
                except (KeyError, TypeError, ValueError) as exc:
                    return json.dumps({"status": "invalid_batch", "message": str(exc)}, ensure_ascii=False)

        @tool
        def measure_reference(requests: list[MeasurementRequest]) -> str:
            """在分析批次之间测量固定参考图, 相同操作复用已有证据.

            Args:
                requests: 测量目的及原图像素坐标下的 crop、region_stats 或 line_profile 规格.
            """
            with self.lock:
                if self.summary_result is not None:
                    return json.dumps({"status": "already_finished"})
                try:
                    return self._measure(requests)
                except (OSError, TypeError, ValueError) as exc:
                    return json.dumps({"status": "invalid_measurement", "message": str(exc)}, ensure_ascii=False)

        @tool
        def finish_analysis(summary: AnalysisSummary, text: str) -> str:
            """收到独立视角报告后, 提交保留证据来源的综合分析.

            Args:
                summary: 结构化综合结果, source_result_ids 必须覆盖所有可用报告.
                text: 简明的整体结论, 同时保留重要的不确定性.
            """
            with self.lock:
                if self.summary_result is not None:
                    return json.dumps({"status": "already_finished"})
                try:
                    return self._finish(summary, text)
                except (KeyError, TypeError, ValueError) as exc:
                    return json.dumps({"status": "invalid_summary", "message": str(exc)}, ensure_ascii=False)

        return [run_analysis_batch, measure_reference, finish_analysis]

    def outcome(self) -> AnalysisOutcome:
        """返回已保存的综合结果及所有已提交业务记录.

        Returns:
            分析运行结果, 也可表示部分完成或未完成.
        """
        return AnalysisOutcome(
            state=self.state, task_id=self.task_id, summary_result=self.summary_result, run_dir=self.directory, stop_reason=self.stop_reason
        )
