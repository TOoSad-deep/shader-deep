"""通过生成闭环验证材料发现、临时回看、错误归档及完成资格."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from langchain_core.exceptions import ModelConnectionError

from shader_deep.agents.generation.agent import _execute
from shader_deep.domain.blackboard import add_result, add_target, add_task
from shader_deep.domain.tasks import ResultRecord, TargetRecord
from shader_deep.workflows.generation import run_generation
from shader_deep.workflows.generation_from_report import run_generation_from_report
from tests.unit_tests.agents.test_context import BASE_CODE, PNG
from tests.unit_tests.agents.test_generation import ThreadBoundRenderer
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, generation_report, task_payloads

if TYPE_CHECKING:
    from shader_deep.agents.generation.tools.session import RenderSession

CODE_A = BASE_CODE + "\n// memory-candidate-a"
CODE_B = BASE_CODE + "\n// memory-candidate-b"
BAD_CODE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(unknown_symbol); } // memory-failed-code"
ERROR_TEXT = "unknown_symbol is not declared"


def memory_receipts(request: dict[str, object]) -> list[dict[str, object]]:
    """读取普通或多模态工具响应的首个 JSON 回执."""
    result = []
    for message in cast("list[dict[str, object]]", request["messages"]):
        if message["role"] != "tool":
            continue
        content = message["content"]
        if isinstance(content, list):
            content = next((block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text"), "")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            result.append(payload)
    return result


def memory_images(request: dict[str, object]) -> list[str]:
    """收集实际序列化请求中的图像 data URL."""
    images = []
    for message in cast("list[dict[str, object]]", request["messages"]):
        if isinstance(message["content"], list):
            images.extend(block["image_url"]["url"] for block in message["content"] if isinstance(block, dict) and block.get("type") == "image_url")
    return images


def finishable(request: dict[str, object]) -> list[str]:
    """读取模型当前实际获知的可选候选清单."""
    for message in cast("list[dict[str, object]]", request["messages"]):
        if message["role"] == "user" and isinstance(message["content"], str):
            try:
                payload = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "finishable_candidates" in payload:
                return payload["finishable_candidates"]
    return []


def material_entries(receipt: dict[str, object]) -> list[dict[str, object]]:
    """取得目录或读取回执中的材料项."""
    return cast("list[dict[str, object]]", receipt["materials"])


def memory_metadata(request: dict[str, object]) -> dict[str, object]:
    """取得生成上下文中的记忆入口."""
    return cast("dict[str, object]", task_payloads(request)[0]["memory"])


class GenerationMemoryTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.options = replace(self.options, visual_memory=True, max_attempts=3, max_model_calls=12)
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)
        self.references: dict[str, dict[str, object]] = {}

    def calls(self, *specifications: tuple[str, dict[str, object]]) -> dict[str, object]:
        messages = [self.call(name, arguments) for name, arguments in specifications]
        calls = [cast("list[dict[str, object]]", message["tool_calls"])[0] for message in messages]
        for index, call in enumerate(calls):
            call["id"] = f"call-{len(self.requests)}-{index}"
        return {"role": "assistant", "content": "", "tool_calls": calls}

    def remember(self, name: str, request: dict[str, object]) -> dict[str, object]:
        self.references[name] = next(
            receipt for receipt in reversed(memory_receipts(request)) if receipt.get("status") in {"rendered", "render_error"}
        )
        return self.references[name]

    def finish(self, reference: dict[str, object]) -> dict[str, object]:
        return self.call("finish_shader", {"candidate_id": reference["candidate_id"], "assessment": "回看后选择已呈现版本"})

    def test_failed_and_old_candidates_are_recalled_then_removed_from_requests(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            step = ("initial", "failed", "a_ready", "b_ready", "listed", "recalled", "finish")[len(self.requests) - 1]
            if step == "initial":
                response = self.call("render_shader", {"glsl_code": BAD_CODE})
            elif step == "failed":
                self.remember("failed", request)
                self.assertEqual(json.dumps(request).count(ERROR_TEXT), 1)
                response = self.call("render_shader", {"glsl_code": CODE_A})
            elif step == "a_ready":
                self.remember("a", request)
                self.assertNotIn(ERROR_TEXT, json.dumps(request))
                self.assertNotIn("memory-failed-code", json.dumps(request))
                response = self.call("render_shader", {"glsl_code": CODE_B})
            elif step == "b_ready":
                self.remember("b", request)
                self.assertNotIn("memory-candidate-a", json.dumps(request))
                self.assertEqual(json.dumps(request).count("memory-candidate-b"), 1)
                response = self.call("list_materials", {"limit": 50})
            elif step == "listed":
                entries = material_entries(memory_receipts(request)[-1])
                self.assertTrue(any(entry["id"] == self.references["failed"]["result_material_id"] for entry in entries))
                response = self.call("read_materials", {"items": self.recall_items()})
            elif step == "recalled":
                self.assertEqual(json.dumps(request).count("memory-candidate-a"), 1)
                self.assertEqual(json.dumps(request).count(ERROR_TEXT), 1)
                self.assertTrue(all(item["status"] == "read" for item in material_entries(memory_receipts(request)[-1])))
                response = self.call("list_materials", {"kind": "code"})
            else:
                self.assertNotIn("memory-candidate-a", json.dumps(request))
                self.assertNotIn(ERROR_TEXT, json.dumps(request))
                response = self.finish(self.references["a"])
            return response

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        if outcome.selected_candidate is None:
            self.fail("成功运行必须交付所选候选")
        self.assertEqual(outcome.selected_candidate.id, self.references["a"]["candidate_id"])
        self.assertEqual(Path(outcome.selected_candidate.code_path).read_text(), CODE_A)
        index = json.loads((outcome.run_dir / "memory" / "index.json").read_text())
        failed = index["materials"][self.references["failed"]["result_material_id"]]
        self.assertIn(ERROR_TEXT, (outcome.run_dir / failed["path"]).read_text())
        self.assertTrue(any(item.get("derived_from") for item in index["materials"].values()))

    def recall_items(self) -> list[dict[str, object]]:
        return [
            {"id": self.references["a"]["code_material_id"]},
            {"id": self.references["a"]["preview_material_id"], "region": {"x": 0, "y": 0, "width": 1, "height": 1}},
            {"id": self.references["failed"]["result_material_id"]},
        ]

    def test_png_input_is_fixed_before_the_first_model_call(self) -> None:
        expected = "data:image/png;base64," + base64.b64encode(PNG).decode()

        def respond(request: dict[str, object]) -> dict[str, object]:
            self.assertEqual(memory_images(request)[0], expected)
            if len(self.requests) == 1:
                (self.root / "reference.PNG").write_bytes(b"replaced external source")
                return self.call("render_shader", {"glsl_code": CODE_A})
            reference = self.remember("a", request)
            if not any("materials" in receipt for receipt in memory_receipts(request)):
                return self.call("read_materials", {"items": [{"id": memory_metadata(request)["reference_id"]}]})
            return self.finish(reference)

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual((self.root / "reference.PNG").read_bytes(), b"replaced external source")

    def test_report_scheme_remains_fixed_while_history_is_compact(self) -> None:
        report = generation_report(self.root)

        def respond(request: dict[str, object]) -> dict[str, object]:
            scheme = cast("dict[str, object]", task_payloads(request)[0]["generation_scheme"])
            self.assertEqual(cast("dict[str, object]", scheme["selected"])["choices"], {"F1": ["M2"]})
            if len(self.requests) == 1:
                return self.call("render_shader", {"glsl_code": CODE_A})
            reference = self.remember("a", request)
            if not any("materials" in receipt for receipt in memory_receipts(request)):
                return self.call("list_materials", {"kind": "json"})
            return self.finish(reference)

        self.response = respond
        outcome = run_generation_from_report(
            report,
            "S1",
            "保持已选方案",
            background="透明",
            alternative=0,
            visual_memory=True,
            max_model_calls=6,
            output_dir=self.options.output_dir,
        )
        self.assertEqual(outcome.stop_reason, "completed")

    def test_read_allowance_is_shared_between_tools_in_the_same_batch(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                identity = memory_metadata(request)["reference_id"]
                items = [{"id": identity}] * 3
                return self.calls(("read_materials", {"items": items}), ("read_materials", {"items": items}))
            reads = [receipt for receipt in memory_receipts(request) if "materials" in receipt]
            statuses = [item["status"] for receipt in reads for item in material_entries(receipt)]
            self.assertEqual(statuses, ["read"] * 4 + ["error"] * 2)
            self.assertEqual(material_entries(reads[-1])[-1]["error"], "read_limit")
            return self.call("stop_generation", {"reason": "额度验证结束", "next_action": "provide_input"})

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(outcome.stop_reason, "blocked")

    def test_finish_rejects_later_read_crop_and_render_in_the_same_batch(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                return self.call("render_shader", {"glsl_code": CODE_A})
            reference = self.remember("a", request)
            self.assertIn(reference["candidate_id"], finishable(request))
            return self.calls(
                ("finish_shader", {"candidate_id": reference["candidate_id"], "assessment": "已看完整预览"}),
                ("read_materials", {"items": [{"id": reference["preview_material_id"], "region": {"x": 0, "y": 0, "width": 1, "height": 1}}]}),
                ("render_shader", {"glsl_code": CODE_B}),
            )

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.attempts, 1)
        index = json.loads((outcome.run_dir / "memory" / "index.json").read_text())
        self.assertFalse(any(item.get("derived_from") for item in index["materials"].values()))
        self.assertFalse((outcome.run_dir / "candidate-002.glsl").exists())

    def test_directory_and_crop_do_not_qualify_an_unpresented_candidate(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            step = ("render", "list", "crop", "full", "finish")[len(self.requests) - 1]
            if step == "render":
                return self.calls(("render_shader", {"glsl_code": CODE_A}), ("render_shader", {"glsl_code": CODE_B}))
            if step == "list":
                rendered = [receipt for receipt in memory_receipts(request) if receipt.get("status") == "rendered"]
                self.references.update(a=rendered[0], b=rendered[1])
                self.assertNotIn(rendered[0]["candidate_id"], finishable(request))
                self.assertIn(rendered[1]["candidate_id"], finishable(request))
                return self.call("list_materials", {"kind": "image"})
            reference = self.references["a"]
            if step in {"crop", "full"}:
                self.assertNotIn(reference["candidate_id"], finishable(request))
                item = {"id": reference["preview_material_id"]}
                if step == "crop":
                    item["region"] = {"x": 0, "y": 0, "width": 1, "height": 1}
                else:
                    self.assertTrue(any(receipt.get("status") == "invalid_selection" for receipt in memory_receipts(request)))
                return self.calls(
                    ("read_materials", {"items": [item]}),
                    ("finish_shader", {"candidate_id": reference["candidate_id"], "assessment": "不能在回看进入请求前选择"}),
                )
            self.assertIn(reference["candidate_id"], finishable(request))
            return self.finish(reference)

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        if outcome.selected_candidate is None:
            self.fail("旧整图实际呈现后应可选择")
        self.assertEqual(outcome.selected_candidate.id, self.references["a"]["candidate_id"])

    def test_failed_model_request_does_not_commit_new_preview_qualification(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                return self.call("render_shader", {"glsl_code": CODE_A})
            reference = self.remember("a", request)
            self.assertIn(reference["candidate_id"], finishable(request))
            msg = "fixture model failed after preview request"
            raise RuntimeError(msg)

        self.response = respond
        with (
            patch("shader_deep.workflows.generation._execute", wraps=_execute) as executed,
            self.assertRaises(ModelConnectionError),
        ):
            run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        session = cast("RenderSession", executed.call_args.args[0])
        self.assertEqual(session.presented, set())
        self.assertIn(self.references["a"]["candidate_id"], session.successful)
        self.assertEqual(session.stop_reason, "error")

    def test_explicit_historical_result_preserves_source_and_judgment_on_readback(self) -> None:
        result = ResultRecord(
            id="old-result",
            task_id="G0",
            status="partial",
            summary="旧版局部检查",
            candidate_ids=("B7",),
            observations=("旧版倒影宽度过大",),
            hypotheses=("可能是坐标映射范围过宽",),
            recommendation="对照旧版局部再判断",
        )
        state = add_result(self.state, result)
        state = add_target(state, TargetRecord(version="T2", request="新一轮修正", reference_path="reference.PNG"))
        state = add_task(state, replace(state["tasks"]["G1"], id="G-related", target_version="T2", related_result_ids=(result.id,)))

        def respond(request: dict[str, object]) -> dict[str, object]:
            metadata = task_payloads(request)[0]
            refs = cast("list[dict[str, str]]", metadata["related_results"])
            self.assertEqual(refs[0]["result_id"], result.id)
            if len(self.requests) == 1:
                self.assertNotIn(result.observations[0], json.dumps(request, ensure_ascii=False))
                return self.call("read_materials", {"items": [{"id": refs[0]["material_id"]}]})
            self.check_historical_result(request, result.id)
            return self.call("stop_generation", {"reason": "已核对历史材料来源", "next_action": "provide_input"})

        self.response = respond
        outcome = run_generation(state, "G-related", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "blocked")
        self.assertEqual(outcome.attempts, 0)

    def check_historical_result(self, request: dict[str, object], result_id: str) -> None:
        objects = []
        for message in cast("list[dict[str, object]]", request["messages"]):
            if message["role"] == "tool" and isinstance(message["content"], list):
                objects.extend(json.loads(block["text"]) for block in message["content"] if isinstance(block, dict) and block.get("type") == "text")
        payload = next(item for item in objects if item.get("id") == result_id)
        self.assertEqual(payload["source_target_version"], "T1")
        self.assertEqual(payload["source_role"], "generation")
        self.assertEqual(payload["observations"], ["旧版倒影宽度过大"])
        self.assertEqual(payload["hypotheses"], ["可能是坐标映射范围过宽"])
