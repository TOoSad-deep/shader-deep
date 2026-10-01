"""单元素、单批独立探索的五库分析公开入口."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.domain.tasks import ResultRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, read_report_package, write_report_package
from shader_deep.workflows.five_analysis import execute_five_analysis
from shader_deep.workflows.options import MAX_ANALYSIS_PERSPECTIVES, MIN_ANALYSIS_TASKS, AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.runtime.task_store import TaskStore
    from shader_deep.workflows.five_analysis import FiveAnalysisResult


def _validate_task(state: BlackboardState, task_id: str, options: AnalysisOptions) -> TaskRecord:
    task = state["tasks"][task_id]
    if task.role != "analysis" or task.lens_config is not None or task.parent_task_id is not None or not task.objective.strip():
        msg = "Expected a root analysis task with a nonblank objective"
        raise ValueError(msg)
    if any(child.parent_task_id == task.id for child in state["tasks"].values()):
        msg = "Create a fresh root task for each analysis run"
        raise ValueError(msg)
    if task.related_result_ids:
        msg = "Five-library analysis requires the original image; historical reports need an explicit conversion"
        raise ValueError(msg)
    if not MIN_ANALYSIS_TASKS <= options.max_tasks <= MAX_ANALYSIS_PERSPECTIVES:
        msg = "Five-library analysis supports one batch of two or three perspectives"
        raise ValueError(msg)
    return task


def _analysis_request(target: TargetRecord, task: TaskRecord) -> str:
    """保留用户原始要求与显式限制, 将任务目标作为补充而非替换."""
    parts = [target.request]
    if target.constraints:
        parts.append("明确约束: " + "; ".join(target.constraints))
    if target.protected_features:
        parts.append("需保护特征: " + "; ".join(target.protected_features))
    if task.objective != target.request:
        parts.append("本次分析目标: " + task.objective)
    return "\n".join(parts)


def _delivery(directory: Path, reference: bytes, result: FiveAnalysisResult, store: TaskStore) -> None:
    if result.libraries is None or result.target_element_id is None:
        return
    manifest = ReportManifest(
        reference=ReferenceAsset(sha256=hashlib.sha256(reference).hexdigest()),
        target_element_id=result.target_element_id,
        status="completed" if result.status == "completed" else "partial",
        open_questions=result.open_questions,
        gaps=result.gaps,
    )
    package = directory / "report"
    if package.exists():
        # 封存前崩溃可能留下已写齐的准备包; 只验证重用, 不覆盖原文件.
        previous_manifest, previous_libraries = read_report_package(package)
        if previous_manifest != manifest or previous_libraries != result.libraries:
            msg = "已有准备包与待封存版本不一致, 拒绝覆盖"
            raise ValueError(msg)
    else:
        write_report_package(package, result.libraries, manifest, reference)
    store.seal(package_path="report")
    log_analysis(
        directory, "报告包已发布并封存", details={"status": manifest.status, "report_dir": str(package), "selected_version": result.selected_version}
    )


def run_analysis_task(
    state: BlackboardState, task_id: str, *, asset_root: Path | None = None, options: AnalysisOptions | None = None
) -> AnalysisOutcome:
    """执行单元素五库分析, 保持公开调用签名与返回记录入口.

    !!! warning "实验性接口"
        默认报告协议为 five_libraries_v1, 完整正文改为文件包及按草图读取.

    Args:
        state: 已登记主分析任务的黑板.
        task_id: 尚未建立子任务的主分析任务标识.
        asset_root: 相对参考图路径的根目录.
        options: 执行配置; 默认三个视角, 不设置总调用额度.

    Returns:
        完整、部分或明确失败的结果; report_dir 指向已发布的完整文件包.

    Raises:
        ValueError: 任务、范围、图片或模型配置无效.
        OSError: 输入或交付文件不可访问.
    """
    options = options or AnalysisOptions()
    task = _validate_task(state, task_id, options)
    target = state["targets"][task.target_version]
    root = asset_root if asset_root is not None else Path.cwd()
    reference_url = png_data_url(root / target.reference_path)
    reference = base64.b64decode(reference_url.split(",", 1)[1])
    model = build_model()
    directory = create_run_directory(options.output_dir)
    (directory / "reference.png").write_bytes(reference)
    delivered: list[FiveAnalysisResult] = []

    def deliver(final: FiveAnalysisResult, store: TaskStore) -> None:
        _delivery(directory, reference, final, store)
        delivered.append(final)

    try:
        result = execute_five_analysis(
            _analysis_request(target, task),
            reference_url,
            options,
            directory,
            model=model,
            on_delivery=deliver,
        )
    except KeyboardInterrupt:
        if delivered:
            _outcome(state, task_id, directory, delivered[-1], options)
        raise
    return _outcome(state, task_id, directory, result, options)


def _outcome(state: BlackboardState, task_id: str, directory: Path, result: FiveAnalysisResult, options: AnalysisOptions) -> AnalysisOutcome:
    summary = None
    package = directory / "report" if result.libraries is not None and (directory / "report").is_dir() else None
    if package is not None:
        summary = ResultRecord(
            id=directory.name + "-five-libraries",
            task_id=task_id,
            status="completed" if result.status == "completed" else "partial",
            summary="五库文件包已交付: report/manifest.json; 按草图读取方案, 候选仍需编码与渲染验证",
            analysis_protocol="five_libraries_v1",
        )
        state = add_result(state, summary)
    # run.json 仅为已提交运行记录的可读投影, 不用来接收任务提交或恢复版本指针.
    commit = json.loads((directory / "commit.json").read_text(encoding="utf-8"))
    option_values = asdict(options)
    option_values["output_dir"] = str(options.output_dir) if options.output_dir is not None else None
    save_run(
        directory,
        state,
        {
            "analysis_protocol": "five_libraries_v1",
            "stop_reason": result.status,
            "report_path": "report" if package is not None else None,
            "selected_version": result.selected_version,
            "target_element_id": result.target_element_id,
            "gaps": [item.model_dump(mode="json") for item in result.gaps],
            "open_questions": [item.model_dump(mode="json") for item in result.open_questions],
            "options": option_values,
            "execution": commit,
        },
    )
    return AnalysisOutcome(state=state, task_id=task_id, summary_result=summary, run_dir=directory, stop_reason=result.status, report_dir=package)


def run_analysis(path: Path, prompt: str, *, options: AnalysisOptions | None = None) -> AnalysisOutcome:
    """分析本地 PNG 的指定元素, 独立视角汇入五库文件包.

    !!! warning "实验性接口"
        报告表达实现假设, completed 不代表编码、渲染或视觉验收通过.

    Args:
        path: 本地 PNG 参考图.
        prompt: 用户要求及目标元素描述.
        options: 可选执行配置.

    Returns:
        本轮状态、黑板结果索引及完整文件包位置.

    Raises:
        ValueError: 提示词或执行配置无效.
    """
    if not prompt.strip():
        msg = "Prompt must not be empty"
        raise ValueError(msg)
    state = add_target(new_blackboard(), TargetRecord(version="T1", request=prompt, reference_path=str(path.resolve())))
    state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective=prompt))
    return run_analysis_task(state, "A1", options=options)
