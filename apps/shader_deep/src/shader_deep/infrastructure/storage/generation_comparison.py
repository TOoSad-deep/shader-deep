"""发布两项生成试验的比较索引, 保存人工选择而不改写子运行."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import quote

from pydantic import TypeAdapter

from shader_deep.domain.generation_comparison import ComparisonItem, GenerationComparison, select_comparison_item
from shader_deep.infrastructure.storage.snapshots import write_snapshot


def _link(directory: Path, path: str) -> str:
    return quote(os.path.relpath(Path(path).resolve(), directory), safe="/.-")


def _item_lines(directory: Path, index: int, item: ComparisonItem) -> list[str]:
    name = " ".join(item.plan.name.split())
    selection = "默认方案" if item.plan.alternative is None else f"局部备选 {item.plan.alternative}"
    lines = [
        f"## {index + 1}. {name}",
        "",
        f"草图: `{item.plan.sketch_id}`; {selection}. 结束原因: `{item.stop_reason}`.",
        f"[运行记录]({_link(directory, str(Path(item.run_dir) / 'run.json'))})",
    ]
    if item.selected_candidate_id is None:
        lines.append("未完成候选选择.")
    else:
        lines.append(f"生成自检选择: `{item.selected_candidate_id}`.")
    if item.code_path is not None:
        lines.append(f"[实际 GLSL]({_link(directory, item.code_path)})")
    if item.preview_path is not None:
        lines.extend([f"[实际预览]({_link(directory, item.preview_path)})", "", f"![{name}预览]({_link(directory, item.preview_path)})"])
    if item.error is not None:
        lines.extend(["", f"运行错误: {' '.join(item.error.split())}"])
    return [*lines, ""]


def _readme(record: GenerationComparison) -> str:
    selection = "尚未选择" if record.selected_item is None else f"第 {record.selected_item + 1} 项, {record.items[record.selected_item].plan.name}"
    lines = [
        "# Shader 方案比较",
        "",
        f"目标: {record.request}",
        f"背景: {record.background}",
        f"共同元素: `{record.element_id}`; 起始基线: `{record.baseline_id}`.",
        f"共同模型: `{record.model_name}`; 渲染: {record.width} x {record.height}, iTime={record.time}; 每项最多 {record.max_attempts} 次尝试.",
        f"报告身份: `{record.report_sha256}`; 原图身份: `{record.reference_sha256}`.",
        "",
        f"人工选择: {selection}. 子运行自检完成不代表用户视觉验收.",
    ]
    if record.selection_reason is not None:
        lines.append(f"选择理由: {record.selection_reason}")
    lines.append("")
    for index, item in enumerate(record.items):
        lines.extend(_item_lines(Path(record.directory), index, item))
    return "\n".join(lines)


def write_generation_comparison(record: GenerationComparison) -> Path:
    """保存比较 JSON 和可打开实际子运行产物的导航.

    !!! warning "实验性接口"
        仅发布两项比较索引, 不复制子运行内容或构建可恢复的执行会话.

    Args:
        record: 两项均已结束的比较记录.

    Returns:
        原子替换写入的 comparison.json 路径; README 另行更新.

    Raises:
        OSError: 比较目录或索引不可写入.
    """
    directory = Path(record.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    record = replace(record, directory=str(directory))
    destination = directory / "comparison.json"
    write_snapshot(destination, asdict(record))
    (directory / "README.md").write_text(_readme(record), encoding="utf-8")
    return destination


def read_generation_comparison(directory: Path) -> GenerationComparison:
    """读取并校验两项比较索引, 不恢复模型或浏览器会话.

    !!! warning "实验性接口"
        只加载比较记录, 子运行的当前选择在人工选择操作时另行核对.

    Args:
        directory: 保存 comparison.json 的目录.

    Returns:
        目录指向本次读取位置的比较记录.

    Raises:
        ValueError: JSON、记录字段或人工选择无效.
        OSError: 比较索引不可读取.
    """
    directory = directory.resolve()
    record = TypeAdapter(GenerationComparison).validate_json((directory / "comparison.json").read_bytes(), strict=True)
    return replace(record, directory=str(directory))


def _verify_child_selection(item: ComparisonItem, candidate_id: str) -> None:
    snapshot = json.loads((Path(item.run_dir) / "run.json").read_text(encoding="utf-8"))
    try:
        candidate = snapshot["blackboard"]["candidates"][candidate_id]
        matches = (
            snapshot["stop_reason"] == "completed"
            and snapshot["selected_candidate_id"] == candidate_id
            and snapshot["task_id"] == item.task_id
            and candidate["id"] == candidate_id
            and candidate["task_id"] == item.task_id
            and candidate["code_path"] == item.code_path
            and candidate["preview_path"] == item.preview_path
        )
    except (KeyError, TypeError):
        matches = False
    if not matches:
        msg = "Comparison selection no longer matches the selected candidate in its child run"
        raise ValueError(msg)


def select_generation_comparison(directory: Path, item_index: int, candidate_id: str, *, reason: str | None = None) -> GenerationComparison:
    """将用户对实际已选候选的明确选择保存到比较索引.

    !!! warning "实验性接口"
        只记录人工选择, 不改写子运行, 不自动开始后续生成.

    Args:
        directory: 已完成两项执行的比较目录.
        item_index: 用户选择的零基比较条目索引.
        candidate_id: 该条目实际已经选中的候选 ID.
        reason: 用户提供的可选选择理由.

    Returns:
        已保存人工选择及理由的比较记录.

    Raises:
        ValueError: 条目未完成、候选不匹配或子运行的选择已改变.
        OSError: 比较索引或明确子运行记录不可访问.
    """
    record = read_generation_comparison(directory)
    item = select_comparison_item(record, item_index)
    if item.selected_candidate_id != candidate_id:
        msg = "Candidate is not the selected candidate of this comparison item"
        raise ValueError(msg)
    _verify_child_selection(item, candidate_id)
    selected = replace(record, selected_item=item_index, selection_reason=reason)
    write_generation_comparison(selected)
    return selected
