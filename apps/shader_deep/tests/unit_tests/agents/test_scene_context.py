"""验证整图请求复用元素素材, 只刷新当前会话的候选与结果."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from shader_deep.agents.generation.context import _build_generation_context
from shader_deep.agents.generation.middleware import GenerationContextMiddleware
from shader_deep.domain.blackboard import add_candidate, add_result, add_target, add_task, new_blackboard
from shader_deep.domain.generation import GenerationBinding
from shader_deep.domain.scene import SceneElementSource, ScenePlan
from shader_deep.domain.tasks import CandidateRecord, ResultRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.generation_inputs import CapturedGenerationInputs
from shader_deep.infrastructure.storage.scene_inputs import CapturedSceneElement, CapturedSceneInputs
from tests.unit_tests.agents.test_context import BASE_CODE, PNG


def _element(root: Path, slot: int) -> CapturedSceneElement:
    directory = root / f"element-{slot}"
    directory.mkdir()
    reference, code, preview = directory / "reference.png", directory / "selected.glsl", directory / "selected.png"
    reference.write_bytes(PNG)
    text = f"{BASE_CODE}\n// element {slot}"
    code.write_text(text, encoding="utf-8")
    buffer = io.BytesIO()
    Image.new("RGBA", (1, 1), (slot * 80, 0, 0, 255)).save(buffer, format="PNG")
    preview.write_bytes(buffer.getvalue())
    report = CapturedGenerationInputs(
        binding=GenerationBinding(
            report_path=str(directory), content_sha256=str(slot) * 64, reference_sha256="a" * 64, element_id="E1", sketch_id="S1"
        ),
        reference_path=str(reference),
        reference_url="data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
        width=1,
        height=1,
        scheme_json=json.dumps({"selected": {"choices": {"F1": ["M1"]}}, "mechanisms": [{"id": "M1", "name": f"元素 {slot} 遮罩"}]}),
        files=(str(reference),),
    )
    return CapturedSceneElement(
        source=SceneElementSource(run_dir=str(root / f"source-{slot}"), candidate_id="C1"),
        task_id="G1",
        inputs=report,
        code=text,
        code_path=str(code),
        preview_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        preview_path=str(preview),
    )


class SceneContextTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        elements = (_element(self.root, 1), _element(self.root, 2))
        plan = ScenePlan(request="合成完整图像", background="透明", layout="保持原图布局与遮挡", elements=tuple(item.source for item in elements))
        self.inputs = CapturedSceneInputs(
            plan=plan,
            elements=elements,
            reference_path=elements[0].inputs.reference_path,
            reference_url=elements[0].inputs.reference_url,
            width=1,
            height=1,
            files=tuple(str(path) for directory in self.root.iterdir() for path in directory.iterdir()),
        )
        state = add_target(new_blackboard(), TargetRecord(version="T1", request=plan.request, reference_path=self.inputs.reference_path))
        self.state = add_task(state, TaskRecord(id="G1", role="generation", target_version="T1", objective="组合整图"))

    def test_cached_sources_keep_identity_and_images_while_scene_candidates_refresh(self) -> None:
        middleware = GenerationContextMiddleware(lambda: self.state, "G1", asset_root=self.root, inputs=self.inputs)
        model = ChatOpenAI(model="fixture-model", api_key="fixture-key", base_url="https://shader-deep.invalid/v1")
        history = [HumanMessage(content="继续组合")]
        request = ModelRequest(model=model, messages=history, state={"messages": history})
        requests: list[ModelRequest] = []

        def capture(prepared: ModelRequest) -> ModelResponse[object]:
            requests.append(prepared)
            return ModelResponse(result=[AIMessage(content="检查整图")])

        middleware.wrap_model_call(request, capture)
        for name in self.inputs.files:
            Path(name).unlink()
        code, preview = self.root / "scene.glsl", self.root / "scene.png"
        code.write_text("scene-code", encoding="utf-8")
        preview.write_bytes(PNG)
        self.state = add_candidate(self.state, CandidateRecord(id="scene-c1", task_id="G1", code_path=str(code), preview_path=str(preview)))
        self.state = add_result(self.state, ResultRecord(id="scene-r1", task_id="G1", status="partial", summary="背景仍需修正"))
        middleware.wrap_model_call(request, capture)
        first, second = (json.loads(item.messages[0].content[0]["text"]) for item in requests)
        self.assertEqual(first["scene_plan"], second["scene_plan"])
        self.assertEqual(first["scene_elements"], second["scene_elements"])
        self.assertEqual([item["slot"] for item in second["scene_elements"]], [1, 2])
        self.assertEqual([item["code"] for item in second["scene_elements"]], [item.code for item in self.inputs.elements])
        self.assertEqual([item["source"]["candidate_id"] for item in second["scene_elements"]], ["C1", "C1"])
        self.assertNotEqual(second["scene_elements"][0]["binding"], second["scene_elements"][1]["binding"])
        self.assertEqual(first["candidates"], [])
        self.assertEqual(second["candidates"][0]["code"], "scene-code")
        self.assertEqual(second["task_results"][0]["summary"], "背景仍需修正")
        source_images = [self.inputs.reference_url, *(item.preview_url for item in self.inputs.elements)]
        for prepared, expected in zip(requests, (source_images, [*source_images, self.inputs.reference_url]), strict=True):
            images = [block["image_url"]["url"] for block in prepared.messages[0].content if block["type"] == "image_url"]
            self.assertEqual(images, expected)
            self.assertEqual(prepared.messages[1:], history)
        context = _build_generation_context(self.state, "G1", asset_root=self.root, inputs=self.inputs)
        self.assertEqual(context.candidate_ids, ("scene-c1",))
        self.assertNotIn("C1", self.state["candidates"])
        self.assertTrue(set(self.inputs.files).issubset(context.files))
        self.assertEqual(request.messages, history)
        self.assertEqual(request.state["messages"], history)

    def test_scene_materials_cannot_be_reused_for_another_target(self) -> None:
        state = add_target(self.state, TargetRecord(version="T2", request="另一个目标", reference_path=self.inputs.reference_path))
        state = add_task(state, replace(self.state["tasks"]["G1"], id="G2", target_version="T2"))
        with self.assertRaisesRegex(ValueError, "do not match the task target"):
            _build_generation_context(state, "G2", asset_root=self.root, inputs=self.inputs)
