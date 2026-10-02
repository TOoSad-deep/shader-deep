"""整图生成消费两个固定元素, 只交付新会话的实际候选."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shader_deep.workflows.scene_generation import run_scene_generation
from tests.unit_tests.agents.test_generation import ThreadBoundRenderer
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, task_payloads, tool_results
from tests.unit_tests.fixtures._scene_fixture import CIRCLE_CODE, RECTANGLE_CODE, SCENE_CODE, scene_sources


class SceneGenerationTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.sources = scene_sources(self.root / "source-inputs")
        self.response = self.scene_response
        ThreadBoundRenderer.renders = []
        ThreadBoundRenderer.closed = 0
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)

    def scene_response(self, request: dict[str, object]) -> dict[str, object]:
        rendered = [result for result in tool_results(request) if result["status"] == "rendered"]
        if rendered:
            return self.call("finish_shader", {"candidate_id": rendered[-1]["candidate_id"], "assessment": "整图模拟自检, 方形位于圆形前方"})
        return self.call("render_shader", {"glsl_code": SCENE_CODE})

    def test_two_scoped_sources_are_materials_and_only_scene_candidate_can_finish(self) -> None:
        before = {path: path.read_bytes() for source in self.sources for path in Path(source.run_dir).rglob("*") if path.is_file()}

        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                return self.call("finish_shader", {"candidate_id": "C1", "assessment": "错误选择源元素"})
            return self.scene_response(request)

        self.response = respond
        outcome = run_scene_generation(self.sources, "组合红圆和绿方形", background="深灰", max_attempts=1, output_dir=self.options.output_dir)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(Path(outcome.selected_candidate.code_path).read_text(), SCENE_CODE)
        self.assertEqual(tool_results(self.requests[1])[0]["status"], "invalid_selection")
        self.assertNotIn("C1", outcome.state["candidates"])
        self.assertEqual(ThreadBoundRenderer.renders, [(32, 24, 0.0)])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        for request in self.requests:
            payload = task_payloads(request)[0]
            self.assertEqual(payload["scene_plan"]["layout"], "保持原图布局与遮挡")
            self.assertEqual([item["code"] for item in payload["scene_elements"]], [CIRCLE_CODE, RECTANGLE_CODE])
            self.assertEqual([item["slot"] for item in payload["scene_elements"]], [1, 2])
            self.assertIsNone(payload["task"]["generation_binding"])
            self.assertEqual(payload["task"]["candidate_ids"], [])
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(len(record["scene_plan"]["elements"]), 2)
        self.assertEqual(len(record["inputs"]["scene_elements"]), 2)
        self.assertNotIn("data:image", json.dumps(record))

    def test_later_requests_reuse_captured_sources_after_original_files_disappear(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                for source in self.sources:
                    for path in Path(source.run_dir).rglob("*"):
                        if path.is_file():
                            path.unlink()
            return self.scene_response(request)

        self.response = respond
        outcome = run_scene_generation(self.sources, "组合两个元素", background="深灰", layout="绿色方形在前", output_dir=self.options.output_dir)
        self.assertEqual(outcome.stop_reason, "completed")
        for request in self.requests:
            payload = task_payloads(request)[0]
            self.assertEqual(payload["scene_plan"]["layout"], "绿色方形在前")
            self.assertEqual([item["code"] for item in payload["scene_elements"]], [CIRCLE_CODE, RECTANGLE_CODE])
        self.assertTrue(list((outcome.run_dir / "inputs").rglob("*.glsl")))

    def test_missing_source_selection_fails_before_model_and_cleans_only_new_run(self) -> None:
        sources = (self.sources[0], replace(self.sources[1], candidate_id="missing"))
        with self.assertRaises(ValueError):
            run_scene_generation(sources, "组合两个元素", background="深灰", output_dir=self.options.output_dir)
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.options.output_dir.iterdir()), [])
        self.assertTrue(all((Path(source.run_dir) / "run.json").is_file() for source in self.sources))
