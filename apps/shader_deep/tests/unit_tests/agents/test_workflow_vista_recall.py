"""生成角色可在后续真实的模型请求中按需回看同一工作流的 PNG 和代码."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from unittest.mock import patch

from PIL import Image

from shader_deep.infrastructure.storage.vista import publish_image
from shader_deep.workflows.generation import run_generation
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results


class WorkflowRecallTests(GenerationFixture):
    def test_selected_historical_png_and_code_reach_next_request(self) -> None:
        vista = self.root / "vista"
        with io.BytesIO() as data, Image.new("RGB", (7, 5), (57, 140, 211)) as historical:
            historical.save(data, format="PNG")
            png = data.getvalue()
        code = "// a previous iteration\nvoid mainImage(out vec4 c, in vec2 p) {c=vec4(0.6);}"
        record = publish_image(vista, label="old-candidate", png=png, code=code.encode(), source_agent="generation")

        def respond(request: dict[str, object]) -> dict[str, object]:
            results = tool_results(request)
            if not results:
                return self.call("list_records", {})
            if "records" in results[-1]:
                self.assertEqual(results[-1]["records"][0]["record"], record)
                return self.call("read_record", {"record": record, "content": "both"})
            self.assertEqual(results[-1]["status"], "read")
            self.assertIn(base64.b64encode(png).decode("ascii"), json.dumps(request))
            self.assertIn(code, [block.get("text") for msg in request["messages"] if isinstance(msg["content"], list) for block in msg["content"]])
            return self.call("stop_generation", {"reason": "历史材料已核对", "next_action": "provide_input"})

        self.response = respond
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=replace(self.options, vista_root=vista))
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(outcome.model_calls, 3)
        self.assertIsNone(outcome.selected_candidate)

    def test_read_after_same_batch_stop_does_not_load_archive_bytes(self) -> None:
        vista = self.root / "vista"
        record = publish_image(vista, label="old", png=(self.root / "reference.PNG").read_bytes())

        def respond(_request: dict[str, object]) -> dict[str, object]:
            response = self.call("stop_generation", {"reason": "已结束", "next_action": "provide_input"})
            read = self.call("read_record", {"record": record})["tool_calls"][0]
            read["id"] = "read-after-stop"
            response["tool_calls"].append(read)
            return response

        self.response = respond
        with patch("shader_deep.agents.generation.memory.read_image") as reader:
            outcome = run_generation(self.state, "G1", asset_root=self.root, options=replace(self.options, vista_root=vista))
        self.assertEqual(outcome.stop_reason, "blocked")
        self.assertEqual(outcome.model_calls, 1)
        reader.assert_not_called()
