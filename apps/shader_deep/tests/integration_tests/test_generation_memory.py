"""用模拟模型与真实 WebGL2 验证旧预览回看和裁剪像素."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from pathlib import Path

from PIL import Image

from shader_deep.workflows.generation import run_generation
from tests.unit_tests.agents.test_generation_memory import finishable, memory_images, memory_receipts
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture

RED_CODE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0, 0.0, 0.0, 1.0); }"
GREEN_CODE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(0.0, 1.0, 0.0, 1.0); }"


class GenerationMemoryIntegrationTests(GenerationFixture):
    def test_old_render_and_crop_reach_request_with_original_pixels(self) -> None:
        self.options = replace(self.options, visual_memory=True, max_attempts=2, max_model_calls=6)
        previous: dict[str, object] = {}

        def respond(request: dict[str, object]) -> dict[str, object]:
            step = ("red", "green", "read", "finish")[len(self.requests) - 1]
            if step == "red":
                return self.call("render_shader", {"glsl_code": RED_CODE})
            if step == "green":
                previous.update(memory_receipts(request)[-1])
                return self.call("render_shader", {"glsl_code": GREEN_CODE})
            if step == "read":
                return self.call(
                    "read_materials",
                    {
                        "items": [
                            {"id": previous["preview_material_id"]},
                            {"id": previous["preview_material_id"], "region": {"x": 3, "y": 4, "width": 5, "height": 6}},
                        ]
                    },
                )
            self.assertIn(previous["candidate_id"], finishable(request))
            return self.call("finish_shader", {"candidate_id": previous["candidate_id"], "assessment": "回看旧整图与局部后选择红色版本"})

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        candidate = outcome.selected_candidate
        if candidate is None or candidate.preview_path is None:
            self.fail("成功运行必须交付实际渲染图")
        self.assertEqual(Path(candidate.code_path).read_text(), RED_CODE)
        raw = Path(candidate.preview_path).read_bytes()
        images = memory_images(self.requests[-1])
        self.assertIn("data:image/png;base64," + base64.b64encode(raw).decode(), images)
        index = json.loads((outcome.run_dir / "memory" / "index.json").read_text())
        derived = next(item for item in index["materials"].values() if item.get("derived_from"))
        crop = (outcome.run_dir / derived["path"]).read_bytes()
        self.assertIn("data:image/png;base64," + base64.b64encode(crop).decode(), images)
        with Image.open(io.BytesIO(raw)) as full, Image.open(io.BytesIO(crop)) as local:
            self.assertEqual(local.size, (5, 6))
            self.assertEqual(local.convert("RGBA").tobytes(), full.convert("RGBA").crop((3, 4, 8, 10)).tobytes())
            self.assertEqual(local.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 255))
