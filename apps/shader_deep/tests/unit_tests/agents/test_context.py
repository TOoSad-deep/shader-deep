"""验证生成上下文的材料选择、文件加载和每轮请求更新."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.generation.context import GenerationContext, build_generation_context
from shader_deep.agents.generation.middleware import GenerationContextMiddleware
from shader_deep.domain.blackboard import add_candidate, add_result, add_target, add_task, new_blackboard
from shader_deep.domain.tasks import BlackboardState, CandidateRecord, ResultRecord, TargetRecord, TaskRecord

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXkAAAAASUVORK5CYII=")
BASE_CODE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(0.25); }"


def fixture_state(root: Path) -> BlackboardState:
    (root / "reference.PNG").write_bytes(PNG)
    (root / "baseline.png").write_bytes(PNG)
    (root / "baseline.glsl").write_text(BASE_CODE, encoding="utf-8")
    state = add_target(
        new_blackboard(),
        TargetRecord(
            version="T1",
            request="复刻参考图",
            reference_path="reference.PNG",
            constraints=("ShaderToy",),
            protected_features=("主体轮廓",),
        ),
    )
    state = add_task(state, TaskRecord(id="G0", role="generation", target_version="T1", objective="粗稿"))
    state = add_candidate(state, CandidateRecord(id="B7", task_id="G0", code_path="baseline.glsl", preview_path="baseline.png"))
    return add_task(
        state,
        TaskRecord(
            id="G1",
            role="generation",
            target_version="T1",
            baseline_id="B7",
            objective="修正倒影宽度",
            hypothesis="横向扰动过强",
            allowed_changes=("倒影扰动",),
            stop_conditions=("形成一个可比较候选",),
        ),
    )


def metadata(context: GenerationContext) -> dict[str, object]:
    return json.loads(context.message.content[0]["text"])


class ContextTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = fixture_state(self.root)

    def test_loads_bound_code_images_and_task_constraints(self) -> None:
        context = build_generation_context(self.state, "G1", asset_root=self.root)
        data = metadata(context)
        self.assertEqual(data["task"]["baseline_id"], "B7")
        self.assertEqual(data["task"]["allowed_changes"], ["倒影扰动"])
        self.assertEqual(data["task"]["hypothesis"], "横向扰动过强")
        self.assertEqual(data["target"]["protected_features"], ["主体轮廓"])
        self.assertEqual(data["candidates"][0]["code"], BASE_CODE)
        images = [block["image_url"]["url"] for block in context.message.content if block["type"] == "image_url"]
        self.assertEqual(images, ["data:image/png;base64," + base64.b64encode(PNG).decode("ascii")] * 2)
        self.assertEqual(set(context.files), {str(self.root / name) for name in ("reference.PNG", "baseline.png", "baseline.glsl")})

    def test_initial_generation_needs_only_reference(self) -> None:
        context = build_generation_context(self.state, "G0", asset_root=self.root)
        self.assertIsNone(context.task.baseline_id)
        state = add_task(self.state, TaskRecord(id="initial", role="generation", target_version="T1", objective="初稿"))
        context = build_generation_context(state, "initial", asset_root=self.root)
        self.assertEqual(context.candidate_ids, ())
        self.assertEqual(context.files, (str(self.root / "reference.PNG"),))

    def test_only_selected_task_material_is_read(self) -> None:
        state = add_task(self.state, TaskRecord(id="unrelated", role="generation", target_version="T1", objective="不相关任务"))
        state = add_candidate(state, CandidateRecord(id="unrelated", task_id="unrelated", code_path="missing-unrelated.glsl"))
        state = add_result(state, ResultRecord(id="unrelated", task_id="unrelated", status="completed", summary="不相关结果"))
        context = build_generation_context(state, "G1", asset_root=self.root)
        self.assertEqual(context.candidate_ids, ("B7",))
        self.assertEqual(context.result_ids, ())
        self.assertNotIn("不相关", context.message.content[0]["text"])

    def test_new_target_does_not_replace_bound_target(self) -> None:
        state = add_target(self.state, TargetRecord(version="T2", request="新目标", reference_path="not-loaded.png"))
        context = build_generation_context(state, "G1", asset_root=self.root)
        self.assertEqual(context.task.target_version, "T1")
        self.assertEqual(metadata(context)["target"]["request"], "复刻参考图")

    def test_historical_result_keeps_its_source_and_uncertainty(self) -> None:
        result = ResultRecord(
            id="R0",
            task_id="G0",
            status="partial",
            summary="旧实验",
            candidate_ids=("B7",),
            observations=("边缘更平滑",),
            hypotheses=("映射范围可能过大",),
            recommendation="检查映射范围",
        )
        state = add_result(self.state, result)
        state = add_target(state, TargetRecord(version="T2", request="新目标", reference_path="reference.PNG"))
        state = add_task(
            state,
            TaskRecord(id="G2", role="generation", target_version="T2", objective="重新试验", baseline_id="B7", related_result_ids=("R0",)),
        )
        data = metadata(build_generation_context(state, "G2", asset_root=self.root))
        self.assertEqual(data["target"]["version"], "T2")
        self.assertEqual(data["candidates"][0]["source_target_version"], "T1")
        self.assertEqual(data["related_results"][0]["source_target_version"], "T1")
        self.assertEqual(data["related_results"][0]["observations"], ["边缘更平滑"])
        self.assertEqual(data["related_results"][0]["hypotheses"], ["映射范围可能过大"])

    def test_repeated_candidate_reference_is_loaded_once(self) -> None:
        state = add_task(self.state, replace(self.state["tasks"]["G1"], id="duplicate-input", candidate_ids=("B7",)))
        context = build_generation_context(state, "duplicate-input", asset_root=self.root)
        self.assertEqual(context.candidate_ids, ("B7",))
        self.assertEqual(len(metadata(context)["candidates"]), 1)
        self.assertEqual(context.files.count(str(self.root / "baseline.png")), 1)

    def test_task_output_and_partial_result_are_visible_without_rebinding(self) -> None:
        (self.root / "candidate.glsl").write_text("candidate-code", encoding="utf-8")
        state = add_candidate(self.state, CandidateRecord(id="C12", task_id="G1", code_path="candidate.glsl"))
        state = add_result(state, ResultRecord(id="R1", task_id="G1", status="partial", summary="编译尚未通过", candidate_ids=("C12",)))
        context = build_generation_context(state, "G1", asset_root=self.root)
        self.assertEqual(context.task.baseline_id, "B7")
        self.assertEqual(context.candidate_ids, ("B7", "C12"))
        self.assertIsNone(metadata(context)["candidates"][1]["preview_path"])
        self.assertEqual(context.result_ids, ("R1",))
        self.assertEqual(metadata(context)["task_results"][0]["status"], "partial")

    def test_declared_missing_preview_is_an_error(self) -> None:
        (self.root / "baseline.png").unlink()
        with self.assertRaisesRegex(ValueError, "PNG file does not exist"):
            build_generation_context(self.state, "G1", asset_root=self.root)

    def test_missing_baseline_code_is_an_error(self) -> None:
        (self.root / "baseline.glsl").unlink()
        with self.assertRaises(FileNotFoundError):
            build_generation_context(self.state, "G1", asset_root=self.root)

    def test_wrong_role_is_rejected_before_loading_images(self) -> None:
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="分析"))
        (self.root / "reference.PNG").unlink()
        with self.assertRaisesRegex(ValueError, "Expected a generation task"):
            build_generation_context(state, "A1", asset_root=self.root)


class MiddlewareTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = fixture_state(self.root)
        self.middleware = GenerationContextMiddleware(lambda: self.state, "G1", asset_root=self.root)
        self.model = ChatOpenAI(model="fixture-model", api_key="fixture-key", base_url="https://shader-deep.invalid/v1")
        self.requests: list[ModelRequest] = []

    def capture(self, request: ModelRequest) -> ModelResponse[object]:
        self.requests.append(request)
        return ModelResponse(result=[AIMessage(content="GLSL")])

    def test_refresh_keeps_tool_history_and_does_not_write_context_to_state(self) -> None:
        history = [
            HumanMessage(content="继续"),
            AIMessage(content="", tool_calls=[{"name": "compile_probe", "args": {}, "id": "call-1"}]),
            ToolMessage(content="ERROR: missing semicolon", tool_call_id="call-1"),
        ]
        request = ModelRequest(model=self.model, messages=history, system_message=SystemMessage(content="角色指令"), state={"messages": history})
        self.middleware.wrap_model_call(request, self.capture)
        self.state = add_result(self.state, ResultRecord(id="R1", task_id="G1", status="partial", summary="工具返回编译错误"))
        self.middleware.wrap_model_call(request, self.capture)
        for prepared in self.requests:
            self.assertEqual(prepared.messages[1:], history)
            self.assertEqual(prepared.system_message, request.system_message)
            self.assertEqual(prepared.tools, request.tools)
            self.assertEqual(sum(message.id == "generation-context:G1" for message in prepared.messages), 1)
        self.assertEqual(request.messages, history)
        self.assertEqual(request.state["messages"], history)
        self.assertEqual(json.loads(self.requests[0].messages[0].content[0]["text"])["task_results"], [])
        self.assertEqual(json.loads(self.requests[1].messages[0].content[0]["text"])["task_results"][0]["id"], "R1")

    def test_async_hook_uses_the_same_materials(self) -> None:
        request = ModelRequest(model=self.model, messages=[HumanMessage(content="继续")])

        async def capture(request: ModelRequest) -> ModelResponse[object]:
            return self.capture(request)

        response = asyncio.run(self.middleware.awrap_model_call(request, capture))
        self.assertEqual(response.result[0].content, "GLSL")
        self.assertEqual(self.requests[0].messages[0].id, "generation-context:G1")
        self.assertEqual(self.requests[0].messages[1:], request.messages)
