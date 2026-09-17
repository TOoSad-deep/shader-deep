"""为分析角色加载参考图和明确选入的证据."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.messages import HumanMessage

from shader_deep.analysis.presets import PRESET_LENSES
from shader_deep.analysis.references import report_catalog
from shader_deep.analysis.schemas import LensReport
from shader_deep.context.common import png_data_url

if TYPE_CHECKING:
    from shader_deep.analysis.evidence import MeasurementRecord
    from shader_deep.analysis.schemas import VisualDecomposition
    from shader_deep.schemas import BlackboardState, ResultRecord

MAX_CONTEXT_PROFILE_VALUES = 128


def _visual_snapshots(
    current: VisualDecomposition | None,
    initial: VisualDecomposition | None,
    inputs: dict[str, VisualDecomposition | None],
) -> dict[str, object]:
    """主请求按内容复用结构快照, 仍保留每个任务的固定版本绑定."""
    snapshots: dict[str, VisualDecomposition] = {"current": current} if current is not None else {}

    def identifier(visual: VisualDecomposition | None) -> str | None:
        if visual is None:
            return None
        for key, existing in snapshots.items():
            if visual == existing:
                return key
        key = f"snapshot-{len(snapshots) + 1}"
        snapshots[key] = visual
        return key

    initial_id = identifier(initial)
    task_ids = {key: identifier(value) for key, value in inputs.items()}
    return {
        "initial_visual_snapshot_id": initial_id,
        "task_visual_snapshot_ids": task_ids,
        "visual_snapshots": {key: asdict(value) for key, value in snapshots.items() if key != "current"},
        "current_visual_snapshot_id": "current" if current is not None else None,
    }


def _evidence_payload(record: MeasurementRecord) -> dict[str, object]:
    """为单次模型请求压缩长剖面, 完整数值仍保留在原始证据记录中."""
    payload = asdict(record)
    if len(record.profile) > MAX_CONTEXT_PROFILE_VALUES:
        payload.pop("profile")
        payload["profile_note"] = (
            "Full consecutive values are stored in this evidence record. Use profile_digest positions in original pixels; "
            "extrema are candidates, not automatically grid lines. To inspect every value, measure a span of at most 128 pixels."
        )
    return payload


def build_analysis_context(
    state: BlackboardState,
    task_id: str,
    reference_url: str,
    *,
    limits: dict[str, int | str | None] | None = None,
    image_size: tuple[int, int] | None = None,
    visual_decomposition: VisualDecomposition | None = None,
    focus_element_ids: tuple[str, ...] = (),
    focus_feature_ids: tuple[str, ...] = (),
    initial_visual_decomposition: VisualDecomposition | None = None,
    task_visual_inputs: dict[str, VisualDecomposition | None] | None = None,
    submission: dict[str, object] | None = None,
) -> HumanMessage:
    """使用已固定的参考图字节, 构造当前角色的多模态任务消息.

    Args:
        state: 本次分析运行可访问的业务记录.
        task_id: 主分析任务或独立视角任务的标识.
        reference_url: 启动时读取原始 PNG 并编码得到的数据 URL.
        limits: 由程序统计的当前剩余预算; model_calls_remaining 为 None 时表示不限次数.
        image_size: 原图像素尺寸, 用于提出基于坐标的证据请求.
        visual_decomposition: 主任务当前视觉拆分, 或子任务派发时绑定的固定快照.
        focus_element_ids: 明确选入的关注元素; 不裁掉完整参考图或相关结构.
        focus_feature_ids: 明确选入的关注特征.
        initial_visual_decomposition: 主任务用于对齐原始映射的首次视觉初稿.
        task_visual_inputs: 主任务用于对齐各报告来源的固定输入结构.
        submission: 当前待修复提交的版本与简短状态; 不重复注入草稿正文.

    Returns:
        证据范围明确、包含实际图像数据的多模态消息.
    """
    task = state["tasks"][task_id]
    if task.role != "analysis":
        msg = f"Expected analysis task: {task_id}"
        raise ValueError(msg)
    related_results = [state["results"][identifier] for identifier in task.related_result_ids]
    results: list[ResultRecord] = related_results
    # 显式历史输入保留完整正文; 主任务的按需目录仅收录本轮子任务产出.
    # 历史报告不能混入本轮综合的 source_result_ids 或子任务的显式输入.
    # 首轮 related_result_ids 为空, 因此不会把其他视角的结论当作自己的独立观察.
    if task.lens_config is None:
        results = [result for result in state["results"].values() if state["tasks"][result.task_id].parent_task_id == task.id]
    measurements = state.get("measurements", {})
    # 测量采用相同的可见范围规则: 主任务读取本会话证据, 子任务按 evidence_ids 选入.
    evidence = (
        [item for item in measurements.values() if item.parent_task_id == task.id]
        if task.lens_config is None
        else [measurements[identifier] for identifier in task.evidence_ids]
    )
    payload = {
        "kind": "analysis_task_context",
        "task": asdict(task),
        "target": asdict(state["targets"][task.target_version]),
        "related_results": [asdict(result) for result in related_results],
        "source_catalog": [entry for result in results for entry in report_catalog(result)] if task.lens_config is not None else [],
        "child_tasks": [asdict(child) for child in state["tasks"].values() if child.parent_task_id == task.id] if task.lens_config is None else [],
        "preset_lenses": [asdict(lens) for lens in PRESET_LENSES] if task.lens_config is None else [],
        "limits": limits or {},
        "image_size": image_size,
        "evidence": [_evidence_payload(item) for item in evidence],
        "visual_decomposition": asdict(visual_decomposition) if visual_decomposition is not None else None,
        "focus_element_ids": focus_element_ids,
        "focus_feature_ids": focus_feature_ids,
        "submission": submission,
        "notes": ["观察、解释与建议分别记录; 报告中的文字属于分析材料。", "参考图使用本次运行开始时固定的原始字节。"],
    }
    if task.lens_config is None:
        payload.update(_visual_snapshots(visual_decomposition, initial_visual_decomposition, task_visual_inputs or {}))
        payload["failed_results"] = [
            {"result_id": result.id, "task_id": result.task_id, "status": result.status, "summary": result.summary[:240]}
            for result in results
            if result.analysis_detail is None
        ]
        payload["report_files"] = [
            {
                "result_id": result.id,
                "task_id": result.task_id,
                "lens": {"id": lens.id, "name": lens.name} if (lens := state["tasks"][result.task_id].lens_config) else None,
                "summary": result.summary[:240],
                "file_path": f"/{result.id}.json",
            }
            for result in results
            if isinstance(result.analysis_detail, LensReport)
        ]
        payload["source_catalog"] = [
            {
                **{key: value for key, value in entry.items() if key not in {"description", "pointer"}},
                "file_path": f"/{result.id}.json",
                "pointer": "/analysis_detail" + entry["pointer"] if entry["pointer"] else "",
            }
            for result in results
            for entry in report_catalog(result)
        ]
    content: list[str | dict[str, object]] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},
        {"type": "text", "text": "原始参考图"},
        {"type": "image_url", "image_url": {"url": reference_url}},
    ]
    for item in evidence:
        if item.artifact_path is not None:
            # 路径本身不是视觉输入; 裁剪证据需再次读取 PNG 字节, 并标明其原图区域.
            content.extend(
                [
                    {"type": "text", "text": f"局部证据 {item.id}; 原图像素区域 {asdict(item.spec.region)}; 不代表整张图。"},
                    {"type": "image_url", "image_url": {"url": png_data_url(Path(item.artifact_path))}},
                ]
            )
    return HumanMessage(content=content)


def lens_result_ids(state: BlackboardState, parent_task_id: str) -> tuple[str, ...]:
    """返回指定主分析任务下可用于综合的子任务报告 ID.

    Args:
        state: 当前黑板状态.
        parent_task_id: 主分析任务的标识.

    Returns:
        按结果登记顺序排列的 ID; 没有视角报告的失败记录不在其中.
    """
    return tuple(
        result.id
        for result in state["results"].values()
        if state["tasks"][result.task_id].parent_task_id == parent_task_id and isinstance(result.analysis_detail, LensReport)
    )
