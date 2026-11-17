"""将生成业务材料接入公共存储, 组织本轮临时回看和固定上下文."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, StrictInt

from shader_deep.agents.generation.context import _candidates, _metadata, _validate_inputs
from shader_deep.agents.generation.history import RenderHistoryReference, project_history
from shader_deep.domain.blackboard import read_task
from shader_deep.infrastructure.storage.visual_memory import MaterialOrigin, MaterialRegion, MaterialSource, VisualMemory

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from shader_deep.domain.tasks import BlackboardState, CandidateRecord, TaskRecords
    from shader_deep.infrastructure.storage.generation_inputs import CapturedGenerationInputs
    from shader_deep.infrastructure.storage.visual_memory import MaterialContent, MaterialKind

MAX_READ_ITEMS = 4


class ReadRegion(BaseModel):
    """工具中的像素区域, 边界和正尺寸由逐项读取校验."""

    model_config = ConfigDict(extra="forbid")
    x: StrictInt
    y: StrictInt
    width: StrictInt
    height: StrictInt


class ReadMaterialRequest(BaseModel):
    """一次完整材料或局部图像读取."""

    model_config = ConfigDict(extra="forbid")
    id: str
    region: ReadRegion | None = None


@dataclass(frozen=True, kw_only=True)
class CandidateMaterials:
    """候选与实际代码、预览及单次结果材料的对应关系."""

    code: str
    preview: str | None = None
    result: str | None = None


def _blocks(content: MaterialContent) -> list[str | dict[str, object]]:
    record = content.record
    label = {"material_id": record.id, "label": record.label, "source": record.source.model_dump(mode="json", exclude_none=True)}
    if content.image_size is not None:
        label["image_size"] = list(content.image_size)
    if record.derived_from is not None:
        label["derived_from"] = record.derived_from.model_dump(mode="json")
    blocks: list[str | dict[str, object]] = [{"type": "text", "text": json.dumps(label, ensure_ascii=False)}]
    if record.kind == "image":
        url = "data:image/png;base64," + base64.b64encode(content.data).decode("ascii")
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    else:
        blocks.append({"type": "text", "text": content.text or ""})
    return blocks


class GenerationMemory:
    """管理一个生成任务的业务映射, 不复制 SDK 模型历史.

    Args:
        state: 本轮固定业务输入.
        task_id: 当前生成任务.
        run_dir: 本轮独立运行目录.
        asset_root: 原业务制品根目录.
        inputs: 已捕获的报告输入, 普通任务时为空.
    """

    def __init__(self, state: BlackboardState, task_id: str, run_dir: Path, asset_root: Path, inputs: CapturedGenerationInputs | None) -> None:
        """固定允许访问的任务材料, 建立可回读引用."""
        self.store = VisualMemory(run_dir, task_id)
        self.task_id, self.run_dir = task_id, run_dir
        self.candidates: dict[str, CandidateMaterials] = {}
        self.rendered_calls: dict[str, RenderHistoryReference] = {}
        self.pending: dict[str, tuple[str, list[MaterialContent]]] = {}
        self.full_candidates: set[str] = set()
        self.latest: str | None = None
        self.failure: str | None = None
        self.read_count = 0
        records = read_task(state, task_id)
        _validate_inputs(records, inputs)
        self.baseline = records.task.baseline_id
        self.fixed = json.loads(_metadata(state, records, (), asset_root, inputs))
        self.reference = self._inputs(records, asset_root, inputs)
        self._result_inputs()

    def _result_inputs(self) -> None:
        """显式历史结果固定为可读 JSON, 入口保留业务身份与材料映射."""
        for category in ("related_results", "task_results"):
            summaries: list[dict[str, str]] = []
            for index, result in enumerate(self.fixed[category], start=1):
                path = self.run_dir / "inputs" / "results" / f"{category}-{index:04d}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                identity = self._input(path, "json", f"输入结果 {result['id']}", origin=MaterialOrigin(task_id=result["task_id"]))
                summaries.append({"result_id": result["id"], "material_id": identity})
            self.fixed[category] = summaries

    def _input(self, path: Path, kind: MaterialKind, label: str, *, origin: MaterialOrigin | None = None) -> str:
        source = MaterialSource(type="input", task_id=self.task_id, role=label, origin=origin)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.run_dir.resolve()):
            target = self.run_dir / "inputs" / "materials" / f"input-{len(self.store) + 1:04d}{path.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resolved.read_bytes())
            resolved = target
        return self.store.register(resolved, kind=kind, label=label, source=source).id

    def _inputs(self, records: TaskRecords, root: Path, inputs: CapturedGenerationInputs | None) -> str:
        reference = Path(inputs.reference_path) if inputs is not None else root / records.target.reference_path
        reference_id = self._input(reference, "image", "reference")
        for candidate in _candidates(records):
            self._candidate_input(candidate, root, inputs)
        if inputs is not None:
            self._report_inputs(inputs, reference)
        # 固定材料在模型启动前验证; 后续仍以已登记副本为准.
        self.store.read(reference_id)
        if self.baseline is not None:
            baseline = self.candidates[self.baseline]
            self.store.read(baseline.code)
            if baseline.preview is not None:
                self.store.read(baseline.preview)
        return reference_id

    def _candidate_input(self, candidate: CandidateRecord, root: Path, inputs: CapturedGenerationInputs | None) -> None:
        baseline = inputs.baseline if inputs is not None else None
        code = Path(baseline.code_path) if baseline is not None and baseline.candidate_id == candidate.id else root / candidate.code_path
        preview = baseline.preview_path if baseline is not None and baseline.candidate_id == candidate.id else candidate.preview_path
        origin = MaterialOrigin(candidate_id=candidate.id, task_id=candidate.task_id)
        code_id = self._input(code, "code", f"输入候选 {candidate.id} 的代码", origin=origin)
        preview_id = self._input(root / preview, "image", f"输入候选 {candidate.id} 的预览", origin=origin) if preview is not None else None
        self.candidates[candidate.id] = CandidateMaterials(code=code_id, preview=preview_id)

    def _report_inputs(self, inputs: CapturedGenerationInputs, reference: Path) -> None:
        origin = MaterialOrigin(**asdict(inputs.binding))
        known = {self.store.get(identity).path for item in self.candidates.values() for identity in (item.code, item.preview) if identity is not None}
        for filename in inputs.files:
            path = Path(filename)
            if path == reference or path.relative_to(self.run_dir).as_posix() in known:
                continue
            kind: MaterialKind = "image" if path.suffix == ".png" else "json" if path.suffix == ".json" else "text"
            self._input(path, kind, f"报告资料 {path.name}", origin=origin)

    def register_code(self, candidate_id: str, path: Path) -> str:
        """源码写入成功后登记, 渲染失败也保留引用."""
        source = MaterialSource(type="output", task_id=self.task_id, operation="render_shader", candidate_id=candidate_id)
        record = self.store.register(path, kind="code", label=f"候选 {candidate_id} 的代码", source=source)
        self.candidates[candidate_id] = CandidateMaterials(code=record.id)
        return record.id

    def record_render(self, candidate_id: str, code: Path, preview: Path | None, error: str | None) -> dict[str, str | None]:
        """由生成侧发布单次结果快照, 返回代码、结果和可选预览引用."""
        source = MaterialSource(type="output", task_id=self.task_id, operation="render_shader", candidate_id=candidate_id)
        code_id = self.candidates[candidate_id].code
        preview_id = None if preview is None else self.store.register(preview, kind="image", label=f"候选 {candidate_id} 的预览", source=source).id
        payload = {
            "candidate_id": candidate_id,
            "result_id": f"{candidate_id}-render",
            "code_material_id": code_id,
            "preview_material_id": preview_id,
            "status": "rendered" if error is None else "render_error",
            "error": error,
        }
        result_path = code.with_name(f"{code.stem}-render.json")
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result_id = self.store.register(result_path, kind="json", label=f"候选 {candidate_id} 的渲染结果", source=source).id
        self.candidates[candidate_id] = CandidateMaterials(code=code_id, preview=preview_id, result=result_id)
        if error is None:
            self.latest = candidate_id
        else:
            self.failure = candidate_id
        return {"code_material_id": code_id, "preview_material_id": preview_id, "result_material_id": result_id}

    def bind_render(self, call_id: str, response: str) -> None:
        """将已执行工具调用与返回的真实材料 ID 关联."""
        payload = json.loads(response)
        if payload.get("status") not in {"rendered", "render_error"} or "result_material_id" not in payload:
            return
        self.rendered_calls[call_id] = RenderHistoryReference(
            candidate_id=payload["candidate_id"],
            code_material_id=payload["code_material_id"],
            result_material_id=payload["result_material_id"],
            preview_material_id=payload.get("preview_material_id"),
            status=payload["status"],
        )

    def read_materials(self, items: list[ReadMaterialRequest], call_id: str) -> str:
        """逐项读取并为下一逻辑轮暂存实际内容, 返回简短回执."""
        results: list[dict[str, object]] = []
        contents: list[MaterialContent] = []
        for item in items:
            self.read_count += 1
            if self.read_count > MAX_READ_ITEMS:
                results.append({"id": item.id, "status": "error", "error": "read_limit", "limit": MAX_READ_ITEMS})
                continue
            try:
                region = MaterialRegion(**item.region.model_dump()) if item.region is not None else None
                content = self.store.read(item.id, region=region)
            except (ValueError, OSError) as exc:
                results.append({"id": item.id, "status": "error", "error": getattr(exc, "code", "read_error"), "message": str(exc)})
                continue
            contents.append(content)
            results.append(
                {
                    "id": item.id,
                    "material_id": content.record.id,
                    "status": "read",
                    "label": content.record.label,
                    "region": item.region.model_dump() if item.region else None,
                    "image_size": content.image_size,
                }
            )
        receipt = json.dumps({"materials": results}, ensure_ascii=False)
        self.pending[call_id] = (receipt, contents)
        return receipt

    def _default_ids(self) -> list[str]:
        identities = [self.reference]
        current = self.latest or self.baseline
        if current is not None:
            material = self.candidates[current]
            identities.extend(identity for identity in (material.code, material.preview) if identity is not None)
        if self.failure is not None:
            failed = self.candidates[self.failure]
            identities.extend(identity for identity in (failed.code, failed.result) if identity is not None)
        return list(dict.fromkeys(identities))

    def _mark_full(self, content: MaterialContent) -> None:
        record = content.record
        candidate_id = record.source.candidate_id
        if candidate_id is not None and record.kind == "image" and record.derived_from is None:
            materials = self.candidates.get(candidate_id)
            if materials is not None and materials.preview == record.id:
                self.full_candidates.add(candidate_id)

    def prepare(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """构建一次请求副本, 不消费待呈现内容或修改 SDK 历史."""
        self.full_candidates = set()
        defaults = self._default_ids()
        payload = {
            **self.fixed,
            "memory": {
                "count": len(self.store),
                "reference_id": self.reference,
                "displayed_material_ids": defaults,
                "instructions": "用 list_materials 查找旧材料, 用 read_materials 按 ID 读取整图、局部或正文.",
            },
        }
        blocks: list[str | dict[str, object]] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]
        for identity in defaults:
            content = self.store.read(identity)
            blocks.extend(_blocks(content))
            self._mark_full(content)
        views = self._read_views(set(defaults))
        context = HumanMessage(content=blocks, id=f"generation-context:{self.task_id}")
        return [context, *project_history(messages, rendered_calls=self.rendered_calls, read_contents=views)]

    def _read_views(self, displayed: set[str]) -> dict[str, list[str | dict[str, object]]]:
        views: dict[str, list[str | dict[str, object]]] = {}
        for call_id, (receipt, contents) in self.pending.items():
            blocks: list[str | dict[str, object]] = [{"type": "text", "text": receipt}]
            for content in contents:
                if content.record.id in displayed:
                    blocks.append({"type": "text", "text": f"材料 {content.record.id} 已在本轮其他位置完整提供, 不重复展开."})
                    continue
                blocks.extend(_blocks(content))
                displayed.add(content.record.id)
                self._mark_full(content)
            views[call_id] = blocks
        return views

    def consumed(self) -> None:
        """成功模型轮次之后释放临时读取内容并开始新的读取额度."""
        self.pending.clear()
        self.failure = None
        self.read_count = 0

    def verified_code(self, candidate_id: str) -> str:
        """交付前按材料身份校验所选源码与完整预览."""
        material = self.candidates[candidate_id]
        if material.preview is not None:
            self.store.read(material.preview)
        return self.store.read(material.code).text or ""
