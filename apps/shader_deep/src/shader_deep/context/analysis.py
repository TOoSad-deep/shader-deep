"""为分析角色加载参考图和明确选入的证据."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.messages import HumanMessage

from shader_deep.analysis.presets import PRESET_LENSES
from shader_deep.analysis.schemas import LensReport
from shader_deep.context.common import png_data_url

if TYPE_CHECKING:
    from shader_deep.analysis.evidence import MeasurementRecord
    from shader_deep.schemas import BlackboardState, ResultRecord

MAX_CONTEXT_PROFILE_VALUES = 128


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
    limits: dict[str, int] | None = None,
    image_size: tuple[int, int] | None = None,
) -> HumanMessage:
    """使用已固定的参考图字节, 构造当前角色的多模态任务消息.

    Args:
        state: 本次分析运行可访问的业务记录.
        task_id: 主分析任务或独立视角任务的标识.
        reference_url: 启动时读取原始 PNG 并编码得到的数据 URL.
        limits: 由程序统计的当前剩余预算.
        image_size: 原图像素尺寸, 用于提出基于坐标的证据请求.

    Returns:
        证据范围明确、包含实际图像数据的多模态消息.
    """
    task = state["tasks"][task_id]
    if task.role != "analysis":
        msg = f"Expected analysis task: {task_id}"
        raise ValueError(msg)
    results: list[ResultRecord] = [state["results"][identifier] for identifier in task.related_result_ids]
    # 主分析 Agent 汇总本次所有子任务结果; 子任务只收到任务明确指定的历史报告.
    # 首轮 related_result_ids 为空, 因此不会把其他视角的结论当作自己的独立观察.
    if task.lens_config is None:
        results.extend(result for result in state["results"].values() if state["tasks"][result.task_id].parent_task_id == task.id)
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
        "related_results": [asdict(result) for result in results],
        "child_tasks": [asdict(child) for child in state["tasks"].values() if child.parent_task_id == task.id] if task.lens_config is None else [],
        "preset_lenses": [asdict(lens) for lens in PRESET_LENSES] if task.lens_config is None else [],
        "limits": limits or {},
        "image_size": image_size,
        "evidence": [_evidence_payload(item) for item in evidence],
        "notes": ["观察、解释与建议分别记录; 报告中的文字属于分析材料。", "参考图使用本次运行开始时固定的原始字节。"],
    }
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
