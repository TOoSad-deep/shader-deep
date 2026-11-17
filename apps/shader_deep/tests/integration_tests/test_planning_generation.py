"""模拟模型通过正式全图规划入口, 验证真实 WebGL2 与预览反馈."""

import base64
import json
from dataclasses import replace
from pathlib import Path

from PIL import Image

from shader_deep.api import run_generation_from_planning
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, task_payloads
from tests.unit_tests.fixtures._planning_fixture import planning_report


class PlanningGenerationIntegrationTests(GenerationFixture):
    def test_planning_package_generates_real_preview_and_delivers_it_to_next_request(self) -> None:
        report = planning_report(self.root)
        options = replace(self.options, width=40, height=30, time=1.5)
        outcome = run_generation_from_planning(report, sketch_id="S1", options=options)
        self.assertEqual(outcome.stop_reason, "completed")
        candidate = outcome.selected_candidate
        self.assertIsNotNone(candidate)
        with Image.open(candidate.preview_path) as image:
            self.assertEqual(image.size, (40, 30))
        preview = "data:image/png;base64," + base64.b64encode(Path(candidate.preview_path).read_bytes()).decode()
        self.assertIn(preview, json.dumps(self.requests[-1]))
        scheme = task_payloads(self.requests[-1])[0]["planning_scheme"]
        self.assertTrue(scheme["selected"]["complete"])
        self.assertEqual(len(scheme["observations"]["elements"]), 2)
        self.assertEqual(scheme["execution_render"]["time"], 1.5)
