"""将生成任务绑定的业务记录和制品组织成模型可见的多模态上下文."""

# 数据流: BlackboardState -> read_task -> 选择候选 -> 加载源码/图片 -> HumanMessage.
# 本模块不调用模型或工具, 也不修改黑板; middleware.py 将结果放进本次模型请求.

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.messages import HumanMessage

from shader_deep.domain.blackboard import read_task
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.storage.scene_inputs import CapturedSceneInputs

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState, CandidateRecord, ResultRecord, TaskRecord, TaskRecords
    from shader_deep.infrastructure.storage.generation_inputs import CapturedBaseline, CapturedGenerationInputs
    from shader_deep.infrastructure.storage.scene_inputs import CapturedSceneElement


@dataclass(frozen=True, kw_only=True)
class GenerationContext:
    """一次调用的生成材料及可核查的来源清单.

    Attributes:
        message: 已实际加载文本与 PNG 的任务消息.
        task: 本次使用的固定任务记录.
        candidate_ids: 本次展开的候选, 包括基线、指定输入和本任务产出.
        result_ids: 本次展开的相关历史结果和本任务结果.
        files: 本轮捕获或本次动态读取的文件绝对路径.
    """

    # 真正发送的材料在 message 中; 其他字段保留任务与文件来源, 供调用方检查.
    message: HumanMessage
    task: TaskRecord
    candidate_ids: tuple[str, ...]
    result_ids: tuple[str, ...]
    files: tuple[str, ...]


def _candidates(records: TaskRecords) -> tuple[CandidateRecord, ...]:
    # 用空元组表示无基线, 方便与其他候选拼接; 单元素元组需要末尾的逗号.
    baseline = (records.baseline,) if records.baseline is not None else ()
    # 顺序为基线、指定输入、本任务产出. 字典按 ID 去重并保留首次插入的位置.
    # 只在历史结果中被提及的候选不会在这里自动展开, 需要显式选为任务输入.
    return tuple({candidate.id: candidate for candidate in (*baseline, *records.inputs, *records.outputs)}.values())


def _candidate_data(state: BlackboardState, candidate: CandidateRecord, root: Path, baseline: CapturedBaseline | None) -> dict[str, object]:
    # 同一候选可能被不同目标下的任务复用, 所以保留其最初生成时的目标和基线.
    origin = state["tasks"][candidate.task_id]
    data = {
        **asdict(candidate),
        "source_target_version": origin.target_version,
        "source_baseline_id": origin.baseline_id,
    }
    if baseline is not None and candidate.id == baseline.candidate_id:
        # 保留原业务记录的路径, 另列本轮实际采用的副本来源.
        data["code"] = baseline.code
        data["captured_code_path"] = baseline.code_path
        data["captured_preview_path"] = baseline.preview_path
    else:
        # 模型不能仅凭本地路径知道代码内容; 新候选仍按当前文件全文装配.
        data["code"] = (root / candidate.code_path).read_text(encoding="utf-8")
    return data


def _result_data(state: BlackboardState, result: ResultRecord) -> dict[str, object]:
    # 标明结论由谁、在什么目标下产生, 避免把历史评审当成当前版本的事实.
    origin = state["tasks"][result.task_id]
    return {
        **asdict(result),
        "source_role": origin.role,
        "source_target_version": origin.target_version,
        "source_baseline_id": origin.baseline_id,
    }


def _metadata(
    state: BlackboardState,
    records: TaskRecords,
    candidates: tuple[CandidateRecord, ...],
    root: Path,
    inputs: CapturedGenerationInputs | CapturedSceneInputs | None,
) -> str:
    # 文字材料用 JSON 保持字段关系; asdict 展开记录, json.dumps 将元组编码为数组.
    # 用户目标、候选源码和结果都在这段文本中, 图片本体则由 _message 单独加入.
    baseline = inputs.baseline if inputs is not None and not isinstance(inputs, CapturedSceneInputs) else None
    payload: dict[str, object] = {
        "kind": "generation_task_context",
        "task": asdict(records.task),
        "target": asdict(records.target),
        "candidates": [_candidate_data(state, candidate, root, baseline) for candidate in candidates],
        "related_results": [_result_data(state, result) for result in records.related_results],
        "task_results": [_result_data(state, result) for result in records.results],
        "notes": [
            "按 task 绑定的目标与起始基线工作, 保护 protected_features, 遵守 allowed_changes 和 stop_conditions.",
            "observations 是观察, hypotheses 是假设, recommendation 是建议; 结果提交和候选登记均不代表已采用.",
            "来源目标或基线不同的历史结论仅作为参考, 不自动适用于本次任务.",
            "preview_path 为空表示没有提供预览; 此上下文不证明代码可编译或预览捕获有效.",
        ],
    }
    if isinstance(inputs, CapturedSceneInputs):
        payload["scene_plan"] = asdict(inputs.plan)
        payload["scene_elements"] = [_scene_element_data(slot, element) for slot, element in enumerate(inputs.elements, start=1)]
    elif inputs is not None:
        payload["generation_scheme"] = json.loads(inputs.scheme_json)
        payload["scheme_instruction"] = "采用 generation_scheme.selected 的有效方案; 条件依据中的其他机制不自动成为本次选择."
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _scene_element_data(slot: int, element: CapturedSceneElement) -> dict[str, object]:
    # 不合并不同来源的短 ID; 槽位、源运行和报告身份共同标识这份素材.
    return {
        "slot": slot,
        "source": asdict(element.source),
        "task_id": element.task_id,
        "binding": asdict(element.inputs.binding),
        "generation_scheme": json.loads(element.inputs.scheme_json),
        "code": element.code,
        "captured_code_path": element.code_path,
        "captured_preview_path": element.preview_path,
    }


def _images(records: TaskRecords, candidates: tuple[CandidateRecord, ...], root: Path) -> tuple[tuple[str, Path], ...]:
    # 参考图始终排第一; 每张候选图前会配文字标签, 让模型知道图与 ID 的对应关系.
    images = [(f"参考图: 目标 {records.target.version}", (root / records.target.reference_path).resolve())]
    for candidate in candidates:
        if candidate.preview_path is not None:
            # None 表示尚无预览, 通常是编译失败的尝试; 其代码与错误仍在文字材料中.
            use = "起始基线" if candidate.id == records.task.baseline_id else "候选"
            images.append((f"{use}预览: {candidate.id}", (root / candidate.preview_path).resolve()))
    return tuple(images)


def _message(metadata: str, images: tuple[tuple[str, Path], ...], task_id: str, cached: dict[Path, str]) -> HumanMessage:
    # 多模态消息的 content 是内容块列表, 可以同时承载文字和实际图片数据.
    content: list[str | dict[str, object]] = [{"type": "text", "text": metadata}]
    for label, path in images:
        content.extend(
            [
                {"type": "text", "text": f"{label}\n文件: {path}"},
                {"type": "image_url", "image_url": {"url": cached[path] if path in cached else png_data_url(path)}},
            ]
        )
    # HumanMessage 表示 user 角色消息, 不表示这些文本来自用户手写.
    # 固定 ID 标识这份任务资料; 避免历史堆积由 middleware 的请求级 override 实现.
    return HumanMessage(content=content, id=f"generation-context:{task_id}")


def _validate_inputs(records: TaskRecords, inputs: CapturedGenerationInputs | CapturedSceneInputs | None) -> None:
    task = records.task
    if task.role != "generation":
        msg = f"Expected a generation task: {task.id}"
        raise ValueError(msg)
    if isinstance(inputs, CapturedSceneInputs):
        _validate_scene_inputs(records, inputs)
        return
    if inputs is None:
        if task.generation_binding is not None:
            msg = "Bound generation task requires prepared inputs"
            raise ValueError(msg)
        return
    baseline_id = inputs.baseline.candidate_id if inputs.baseline is not None else None
    if inputs.binding != task.generation_binding or baseline_id != task.baseline_id:
        msg = "Prepared generation inputs do not match task binding or baseline"
        raise ValueError(msg)


def _validate_scene_inputs(records: TaskRecords, inputs: CapturedSceneInputs) -> None:
    task = records.task
    if task.generation_binding is not None or task.baseline_id is not None or task.candidate_ids:
        msg = "Scene task cannot bind a single report, baseline or historical candidates"
        raise ValueError(msg)
    if records.target.reference_path != inputs.reference_path or records.target.request != inputs.plan.request:
        msg = "Prepared scene inputs do not match the task target"
        raise ValueError(msg)


def _captured_images(
    records: TaskRecords, candidates: tuple[CandidateRecord, ...], root: Path, inputs: CapturedGenerationInputs
) -> tuple[tuple[tuple[str, Path], ...], dict[Path, str]]:
    reference = Path(inputs.reference_path)
    images = [(f"参考图: 目标 {records.target.version}", reference)]
    cached = {reference: inputs.reference_url}
    for candidate in candidates:
        if inputs.baseline is not None and candidate.id == inputs.baseline.candidate_id:
            baseline = inputs.baseline
            if baseline.preview_path is not None and baseline.preview_url is not None:
                path = Path(baseline.preview_path)
                images.append((f"起始基线预览: {candidate.id}", path))
                cached[path] = baseline.preview_url
        elif candidate.preview_path is not None:
            images.append((f"候选预览: {candidate.id}", (root / candidate.preview_path).resolve()))
    return tuple(images), cached


def _scene_images(
    records: TaskRecords, candidates: tuple[CandidateRecord, ...], root: Path, inputs: CapturedSceneInputs
) -> tuple[tuple[tuple[str, Path], ...], dict[Path, str]]:
    reference = Path(inputs.reference_path)
    images = [(f"完整参考图: 目标 {records.target.version}", reference)]
    cached = {reference: inputs.reference_url}
    for slot, element in enumerate(inputs.elements, start=1):
        if element.preview_path is not None and element.preview_url is not None:
            preview = Path(element.preview_path)
            label = f"元素素材 {slot} 预览: 来源 {element.source.run_dir}, 候选 {element.source.candidate_id}"
            images.append((label, preview))
            cached[preview] = element.preview_url
    images.extend(
        (f"整图候选预览: {candidate.id}", (root / candidate.preview_path).resolve()) for candidate in candidates if candidate.preview_path is not None
    )
    return tuple(images), cached


def _image_materials(
    records: TaskRecords, candidates: tuple[CandidateRecord, ...], root: Path, inputs: CapturedGenerationInputs | CapturedSceneInputs | None
) -> tuple[tuple[tuple[str, Path], ...], dict[Path, str]]:
    if isinstance(inputs, CapturedSceneInputs):
        return _scene_images(records, candidates, root, inputs)
    if inputs is not None:
        return _captured_images(records, candidates, root, inputs)
    return _images(records, candidates, root), {}


def _build_generation_context(
    state: BlackboardState,
    task_id: str,
    *,
    asset_root: Path | None = None,
    inputs: CapturedGenerationInputs | CapturedSceneInputs | None = None,
) -> GenerationContext:
    records = read_task(state, task_id)
    _validate_inputs(records, inputs)
    root = (asset_root if asset_root is not None else Path.cwd()).resolve()
    candidates = _candidates(records)
    images, cached = _image_materials(records, candidates, root, inputs)
    metadata = _metadata(state, records, candidates, root, inputs)
    baseline = inputs.baseline if inputs is not None and not isinstance(inputs, CapturedSceneInputs) else None
    files = [*(inputs.files if inputs is not None else ()), *(str(path) for _, path in images)]
    files.extend(
        baseline.code_path if baseline is not None and candidate.id == baseline.candidate_id else str((root / candidate.code_path).resolve())
        for candidate in candidates
    )
    return GenerationContext(
        message=_message(metadata, images, task_id, cached),
        task=records.task,
        candidate_ids=tuple(candidate.id for candidate in candidates),
        result_ids=tuple(dict.fromkeys(result.id for result in (*records.related_results, *records.results))),
        files=tuple(dict.fromkeys(files)),
    )


def build_generation_context(state: BlackboardState, task_id: str, *, asset_root: Path | None = None) -> GenerationContext:
    """从黑板读取未绑定报告的生成任务, 加载代码与图像并保留证据来源.

    Args:
        state: 本次读取的黑板状态.
        task_id: 要执行的生成任务标识.
        asset_root: 相对制品路径的根目录, 未提供时使用当前工作目录.

    Returns:
        已加载的任务消息与本次材料清单, 不修改黑板状态.

    Raises:
        KeyError: 任务或所引用的业务记录不存在.
        ValueError: 角色不是生成、任务需要报告准备材料, 或所需 PNG 路径无效.
        OSError: 绑定代码或图像文件无法读取.
    """
    return _build_generation_context(state, task_id, asset_root=asset_root)
