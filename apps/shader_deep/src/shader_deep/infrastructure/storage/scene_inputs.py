"""从已选元素运行固定整图输入, 不将来源候选登记为整图候选."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.generation import GenerationBinding
from shader_deep.domain.tasks import CandidateRecord
from shader_deep.infrastructure.storage.generation_inputs import CapturedGenerationInputs, _png_url, _preview_bytes, capture_generation_inputs

if TYPE_CHECKING:
    from shader_deep.domain.scene import SceneElementSource, ScenePlan


@dataclass(frozen=True, kw_only=True)
class CapturedSceneElement:
    """来源身份和本轮独立副本, 代码与预览在内存中固定."""

    source: SceneElementSource
    task_id: str
    inputs: CapturedGenerationInputs
    code: str
    code_path: str
    preview_url: str | None
    preview_path: str | None


@dataclass(frozen=True, kw_only=True)
class CapturedSceneInputs:
    """整图请求逐轮复用的材料, 不含旧黑板或来源会话历史."""

    plan: ScenePlan
    elements: tuple[CapturedSceneElement, ...]
    reference_path: str
    reference_url: str
    width: int
    height: int
    files: tuple[str, ...]


def _read_source(source: SceneElementSource) -> tuple[SceneElementSource, CandidateRecord, GenerationBinding]:
    directory = Path(source.run_dir).resolve()
    snapshot = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    try:
        data = snapshot["blackboard"]["candidates"][source.candidate_id]
        task = snapshot["blackboard"]["tasks"][data["task_id"]]
        matches = (
            snapshot["stop_reason"] == "completed"
            and snapshot["selected_candidate_id"] == source.candidate_id == data["id"]
            and snapshot["task_id"] == data["task_id"] == task["id"]
            and task["role"] == "generation"
            and task["generation_binding"] is not None
        )
        if matches:
            candidate = CandidateRecord(id=data["id"], task_id=data["task_id"], code_path=data["code_path"], preview_path=data.get("preview_path"))
            return replace(source, run_dir=str(directory)), candidate, GenerationBinding(**task["generation_binding"])
    except (KeyError, TypeError):
        pass
    msg = f"Scene source must identify the selected candidate of a completed report generation: {directory}"
    raise ValueError(msg)


def _capture_element(source: SceneElementSource, directory: Path) -> CapturedSceneElement:
    source, candidate, binding = _read_source(source)
    root = Path(source.run_dir)
    inputs = capture_generation_inputs(root / binding.report_path, directory, binding.sketch_id, alternative=binding.alternative)
    # 仅副本路径允许改变; 原报告正文、原图或选定来源变化不能沿用旧绑定身份.
    if replace(inputs.binding, report_path=binding.report_path) != binding:
        msg = f"Scene source report no longer matches its generation binding: {source.run_dir}"
        raise ValueError(msg)
    code = (root / candidate.code_path).read_bytes()
    text = code.decode("utf-8")
    preview = _preview_bytes(candidate.preview_path, root)
    code_path = directory / "selected.glsl"
    code_path.write_bytes(code)
    preview_path = directory / "selected.png" if preview is not None else None
    if preview_path is not None and preview is not None:
        preview_path.write_bytes(preview)
    return CapturedSceneElement(
        source=source,
        task_id=candidate.task_id,
        inputs=inputs,
        code=text,
        code_path=str(code_path),
        preview_url=_png_url(preview) if preview is not None else None,
        preview_path=str(preview_path) if preview_path is not None else None,
    )


def _validate_common_reference(elements: tuple[CapturedSceneElement, ...]) -> None:
    if len({element.inputs.binding.reference_sha256 for element in elements}) != 1:
        msg = "Scene element sources must use the same complete reference image"
        raise ValueError(msg)
    identities = {(element.inputs.binding.content_sha256, element.inputs.binding.element_id) for element in elements}
    if len(identities) != len(elements):
        msg = "Two schemes for the same report element cannot be composed as distinct elements"
        raise ValueError(msg)


def capture_scene_inputs(plan: ScenePlan, run_dir: Path) -> CapturedSceneInputs:
    """固定两个已选元素的报告、代码和预览, 校验它们来自同一完整原图.

    !!! warning "实验性接口"
        首版只接受两个完成的报告生成运行, 不接受无来源代码或跨图素材.

    Args:
        plan: 本次整图目标及明确的两个元素来源.
        run_dir: 工作流已经分配的本次整图运行目录.

    Returns:
        规范化来源、固定文件副本及每轮可复用的文本和图像材料.

    Raises:
        ValueError: 来源未完成、选择或报告身份不匹配、原图不同或元素重复.
        OSError: 来源记录或所需制品不可读取, 或副本不可写入.
    """
    root = run_dir.resolve() / "inputs"
    elements = tuple(_capture_element(source, root / f"element-{index}") for index, source in enumerate(plan.elements, start=1))
    _validate_common_reference(elements)
    first = elements[0].inputs
    files = tuple(path for element in elements for path in (*element.inputs.files, element.code_path, element.preview_path) if path is not None)
    return CapturedSceneInputs(
        plan=replace(plan, elements=tuple(element.source for element in elements)),
        elements=elements,
        reference_path=first.reference_path,
        reference_url=first.reference_url,
        width=first.width,
        height=first.height,
        files=files,
    )


def scene_input_records(inputs: CapturedSceneInputs) -> list[dict[str, object]]:
    """提取轻量来源映射供整图 run.json 保存, 不复制代码、图像或方案正文.

    Args:
        inputs: 已固定并验证的整图材料.

    Returns:
        每个元素的源运行、任务、报告绑定及本轮制品路径.
    """
    return [
        {
            "source": asdict(element.source),
            "task_id": element.task_id,
            "binding": asdict(element.inputs.binding),
            "code_path": element.code_path,
            "preview_path": element.preview_path,
        }
        for element in inputs.elements
    ]
