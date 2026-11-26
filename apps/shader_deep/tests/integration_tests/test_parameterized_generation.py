"""验证候选寻优、真实图像回读与两份 GLSL 交付."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

from shader_deep.rendering import WebGL2Renderer
from shader_deep.workflows.generation import run_shader
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results

_GLSL = """
uniform float uLevel;
void mainImage(out vec4 color, in vec2 p) {
    float spot = exp(-length(p - .5 * iResolution.xy) * .18);
    color = vec4(.1 + uLevel * spot, .1 + uLevel * spot, .1 + uLevel * spot, 1.);
}
"""


class ParameterizedGenerationTests(GenerationFixture):
    def test_real_optimizer_winner_requires_next_request_and_reuses_baseline(self) -> None:
        with WebGL2Renderer() as renderer:
            png = renderer.render(_GLSL, 32, 24, 0, uniforms={"uLevel": 0.75})
        reference = self.root / "target.png"
        reference.write_bytes(png)

        def respond(request: dict[str, object]) -> dict[str, object]:
            rendered = [item for item in tool_results(request) if item["status"] == "rendered"]
            if not rendered:
                return self.call(
                    "render_shader",
                    {
                        "glsl_code": _GLSL,
                        "parameter_specs": [{"name": "uLevel", "type": "float", "default": 0.2, "min": 0.1, "max": 0.95}],
                    },
                )
            if len(rendered) == 1:
                baseline_id = rendered[0]["candidate_id"]
                message = self.call("optimize_uniforms", {"baseline_id": baseline_id, "parameter_names": ["uLevel"]})
                call = self.call("finish_shader", {"candidate_id": str(baseline_id).replace("c001", "c002"), "assessment": "尚未看图"})
                call["tool_calls"][0]["id"] = "premature"
                message["tool_calls"].extend(call["tool_calls"])
                return message
            winner = rendered[1]
            preview = Path(winner["preview_path"]).read_bytes()
            self.assertIn(base64.b64encode(preview).decode(), json.dumps(request))
            self.assertTrue(any(item["status"] == "invalid_selection" for item in tool_results(request)))
            return self.call("finish_shader", {"candidate_id": winner["candidate_id"], "assessment": "已看到这一候选的预览"})

        self.response = respond
        options = replace(self.options, optimize_parameters=True)
        outcome = run_shader(reference, "复现软发光点", options=options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.attempts, 2)
        winner = outcome.selected_candidate
        self.assertIsNotNone(winner)
        self.assertIsNotNone(outcome.baked_code_path)
        candidates = list(outcome.state["candidates"].values())
        self.assertEqual(candidates[0].code_path, winner.code_path)
        record = json.loads(Path(winner.render_record_path).read_text())
        self.assertAlmostEqual(record["render"]["uniforms"]["uLevel"], 0.75, delta=0.02)
        self.assertEqual(record["glsl"], Path(winner.code_path).name)
        self.assertNotIn("uniform float uLevel", outcome.baked_code_path.read_text())
        with WebGL2Renderer() as renderer:
            self.assertEqual(renderer.render(outcome.baked_code_path.read_text(), 32, 24, 0), Path(winner.preview_path).read_bytes())
