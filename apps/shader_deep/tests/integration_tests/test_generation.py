"""使用模拟模型与真实 WebGL2, 验证单 Agent 的错误修复和图像反馈."""

import base64
import json
from pathlib import Path

from PIL import Image

from shader_deep.domain.generation_comparison import GenerationPlan
from shader_deep.domain.scene import SceneElementSource
from shader_deep.workflows.generation import run_generation
from shader_deep.workflows.generation_comparison import run_generation_comparison
from shader_deep.workflows.generation_from_report import run_generation_from_report
from shader_deep.workflows.scene_generation import run_scene_generation
from tests.unit_tests.agents.test_context import BASE_CODE
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, generation_report, task_payloads, tool_results
from tests.unit_tests.fixtures._scene_fixture import CIRCLE_CODE, RECTANGLE_CODE, SCENE_CODE, scene_reports


class GenerationIntegrationTests(GenerationFixture):
    def test_compile_error_repair_real_preview_and_selection(self) -> None:
        self.response = self.repair_then_finish
        report = generation_report(self.root)
        outcome = run_generation_from_report(
            report,
            "S1",
            "只实现粉色圆形",
            background="透明",
            alternative=0,
            state=self.state,
            baseline_id="B7",
            asset_root=self.root,
            max_attempts=self.options.max_attempts,
            output_dir=self.options.output_dir,
        )
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.model_calls, 3)
        self.assertIn("unknown_symbol", tool_results(self.requests[1])[0]["error"])
        candidate = outcome.selected_candidate
        self.assertEqual(Path(candidate.code_path).read_text(), BASE_CODE)
        png = Path(candidate.preview_path).read_bytes()
        with Image.open(candidate.preview_path) as image:
            self.assertEqual(image.size, (32, 24))
        expected = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        images = [
            block["image_url"]["url"]
            for message in self.requests[2]["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if block.get("type") == "image_url"
        ]
        self.assertIn(expected, images)
        self.assertEqual(task_payloads(self.requests[2])[0]["task"]["baseline_id"], "B7")
        for request in self.requests:
            payload = task_payloads(request)[0]
            self.assertEqual(payload["generation_scheme"]["selected"]["choices"], {"F1": ["M2"]})
            self.assertEqual(payload["task"]["generation_binding"]["alternative"], 0)
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(record["selected_candidate_id"], candidate.id)
        self.assertEqual(record["inputs"]["baseline"]["candidate_id"], "B7")
        self.assertEqual(Path(record["inputs"]["baseline"]["code_path"]).read_text(), BASE_CODE)
        self.assertEqual(list(self.options.output_dir.iterdir()), [outcome.run_dir])
        self.assertTrue((outcome.run_dir / "inputs" / "report" / "manifest.json").is_file())

    def test_real_render_failures_stop_with_preserved_code(self) -> None:
        self.response = lambda _request: self.call("render_shader", {"glsl_code": "invalid_glsl"})
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertIsNone(outcome.selected_candidate)
        self.assertEqual(outcome.stop_reason, "attempt_limit")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(list(outcome.run_dir.glob("*.glsl"))), 2)
        self.assertEqual(list(outcome.run_dir.glob("*.png")), [])
        self.assertEqual(json.loads((outcome.run_dir / "run.json").read_text())["stop_reason"], "attempt_limit")

    def test_two_plans_deliver_independent_real_previews(self) -> None:
        report = generation_report(self.root)
        comparison = run_generation_comparison(
            report,
            (GenerationPlan(name="默认", sketch_id="S1"), GenerationPlan(name="高斯备选", sketch_id="S1", alternative=0)),
            "只实现粉色圆形",
            background="透明",
            max_attempts=1,
            output_dir=self.options.output_dir,
        )
        self.assertIsNone(comparison.selected_item)
        self.assertEqual(len({item.run_dir for item in comparison.items}), 2)
        self.assertTrue((Path(comparison.directory) / "comparison.json").is_file())
        for index, item in enumerate(comparison.items):
            self.assertEqual(item.stop_reason, "completed")
            self.assertEqual(Path(item.code_path).read_text(), BASE_CODE)
            with Image.open(item.preview_path) as image:
                self.assertEqual(image.size, (32, 24))
            request = self.requests[index * 2 + 1]
            expected = "data:image/png;base64," + base64.b64encode(Path(item.preview_path).read_bytes()).decode("ascii")
            images = [
                block["image_url"]["url"]
                for message in request["messages"]
                if isinstance(message["content"], list)
                for block in message["content"]
                if block.get("type") == "image_url"
            ]
            self.assertIn(expected, images)

    def test_two_rendered_elements_form_a_new_overlapping_scene_candidate(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            rendered = [result for result in tool_results(request) if result["status"] == "rendered"]
            if rendered:
                return self.call("finish_shader", {"candidate_id": rendered[-1]["candidate_id"], "assessment": "模拟模型自检, 待用户验收"})
            payload = task_payloads(request)[0]
            if "scene_plan" in payload:
                code = SCENE_CODE
            else:
                name = payload["generation_scheme"]["reference_content"]["elements"][0]["name"]
                code = CIRCLE_CODE if name == "红色圆形" else RECTANGLE_CODE
            return self.call("render_shader", {"glsl_code": code})

        self.response = respond
        sources = []
        for report in scene_reports(self.root / "scene-inputs"):
            outcome = run_generation_from_report(report, "S1", "实现当前元素", background="深灰", max_attempts=1, output_dir=self.root / "elements")
            sources.append(SceneElementSource(run_dir=str(outcome.run_dir), candidate_id=outcome.selected_candidate.id))
        outcome = run_scene_generation(sources, "组合红圆与绿方形", background="深灰", max_attempts=1, output_dir=self.options.output_dir)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(Path(outcome.selected_candidate.code_path).read_text(), SCENE_CODE)
        self.assertNotIn(outcome.selected_candidate.id, {source.candidate_id for source in sources})
        with Image.open(outcome.selected_candidate.preview_path) as image:
            self.assertEqual(image.size, (32, 24))
            self.assertEqual(image.convert("RGB").getpixel((7, 12)), (255, 0, 0))
            self.assertEqual(image.convert("RGB").getpixel((18, 12)), (0, 255, 0))
        requests = [request for request in self.requests if "scene_plan" in task_payloads(request)[0]]
        self.assertEqual([item["code"] for item in task_payloads(requests[0])[0]["scene_elements"]], [CIRCLE_CODE, RECTANGLE_CODE])
        expected = "data:image/png;base64," + base64.b64encode(Path(outcome.selected_candidate.preview_path).read_bytes()).decode("ascii")
        self.assertIn(expected, json.dumps(requests[-1]))
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(len(record["scene_plan"]["elements"]), 2)
