"""使用模拟模型与真实 WebGL2, 验证单 Agent 的错误修复和图像反馈."""

import base64
import json
from pathlib import Path

from PIL import Image

from shader_deep.workflows.generation import run_generation
from shader_deep.workflows.generation_from_report import run_generation_from_report
from tests.unit_tests.agents.test_context import BASE_CODE
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, generation_report, task_payloads, tool_results


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
