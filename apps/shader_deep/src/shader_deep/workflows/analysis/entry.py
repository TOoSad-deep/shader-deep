"""单元素、单批独立探索的五库分析公开入口."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from shader_deep.domain.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.domain.tasks import ResultRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.storage.analysis_materials import package_materials
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.report_evidence import EvidenceAsset, evidence_bytes
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from shader_deep.workflows.analysis.pipeline import execute_five_analysis
from shader_deep.workflows.options import MAX_ANALYSIS_PERSPECTIVES, MIN_ANALYSIS_TASKS, AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.workflows.analysis.contracts import FiveAnalysisResult
    from shader_deep.workflows.analysis.session import AnalysisSession


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


def _delivery(directory: Path, reference: bytes, result: FiveAnalysisResult, session: AnalysisSession) -> None:
    """从已选合法产物发布消费者兼容文件包, 不封存执行恢复协议."""
    if result.libraries is None or result.target_element_id is None or result.selected_version is None:
        return
    # 五库正文、问题与证据均取自同一选定版本, 避免混用 V0 和 V1 的引用.
    selected = session.versions[result.selected_version]
    materials = TypeAdapter(dict[str, JsonValue]).validate_python(selected.get("materials", {}))
    digest = hashlib.sha256(reference).hexdigest()
    evidence, assets = package_materials(materials, digest)
    declaration = EvidenceAsset(path="evidence.json", sha256=hashlib.sha256(evidence_bytes(evidence)).hexdigest()) if evidence is not None else None
    manifest = ReportManifest(
        reference=ReferenceAsset(sha256=digest),
        target_element_id=result.target_element_id,
        status="completed" if result.status == "completed" else "partial",
        evidence=declaration,
        context_notes=TypeAdapter(tuple[str, ...]).validate_python(materials.get("context_notes", [])),
        open_questions=result.open_questions,
        gaps=result.gaps,
    )
    write_report_package(directory / "report", result.libraries, manifest, reference, evidence=evidence, evidence_assets=assets)
    log_analysis(
        directory,
        "报告包已发布",
        details={"status": manifest.status, "report_dir": str(directory / "report"), "selected_version": result.selected_version},
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
        options: 执行配置; 默认三个视角, 原生图各自受框架限制.

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
    # 启动时只读取一次原文件; 模型输入、运行副本和交付哈希复用这份固定字节.
    reference_url = png_data_url(root / target.reference_path)
    reference = base64.b64decode(reference_url.split(",", 1)[1])
    model = build_model()
    directory = create_run_directory(options.output_dir)
    (directory / "reference.png").write_bytes(reference)
    delivered: list[FiveAnalysisResult] = []

    def deliver(final: FiveAnalysisResult, session: AnalysisSession) -> None:
        _delivery(directory, reference, final, session)
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
        # 工作流先完成合法产物交付再重抛中断, 这里补存公开结果索引后继续传播取消.
        if delivered:
            _outcome(state, task_id, directory, delivered[-1], options)
        raise
    return _outcome(state, task_id, directory, result, options)


def _outcome(state: BlackboardState, task_id: str, directory: Path, result: FiveAnalysisResult, options: AnalysisOptions) -> AnalysisOutcome:
    summary = None
    package = directory / "report" if result.libraries is not None and (directory / "report").is_dir() else None
    if package is not None:
        # 公开黑板只登记交付索引; 五库正文由文件包承载, 不复制为第二份权威报告.
        summary = ResultRecord(
            id=directory.name + "-five-libraries",
            task_id=task_id,
            status="completed" if result.status == "completed" else "partial",
            summary="五库文件包已交付: report/manifest.json; 按草图读取方案, 候选仍需编码与渲染验证",
            analysis_protocol="five_libraries_v1",
        )
        state = add_result(state, summary)
    # run.json 只保存本轮业务记录, 不恢复图的消息或执行.
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
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
            "execution": manifest,
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
