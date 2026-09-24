"""验证后续材料包在测量额度耗尽后仍能批量复用完整裁剪证据."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.transport import configure_analysis_model
from shader_deep.analysis.types import AnalysisOptions
from shader_deep.blackboard import add_task
from shader_deep.config import build_model
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TaskRecord
from tests.unit_tests._generation_fixture import GenerationFixture, tool_results
from tests.unit_tests.test_possibility_library import outline, report


class IntegrationEvidenceReuseTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        reference = self.root / "reference.PNG"
        with Image.new("RGB", (8, 6), "white") as image:
            image.paste("blue", (4, 0, 8, 6))
            image.save(reference)
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="复刻参考图"))
        options = AnalysisOptions(max_measurements=2, max_integration_calls=4, max_request_retries=0)
        self.session = ExplorationSession(state, "A1", options, self.root, png_data_url(reference))
        self.session._submit_outline(outline(), ["平面组织", "空间形态"])
        for task in self.session._register():
            self.session._commit_report(task, report())
        targets = [item.id for item in self.session.store.library.feature_library]
        self.session._read_library(
            {
                "comparison": {"question": "比较拥有者", "target_ids": targets},
                "requests": [{"library": "feature_library", "ids": targets}],
            }
        )
        self.session._measurement_tool().invoke(
            {
                "requests": [
                    {"question": "比较局部", "measurement": {"kind": "crop", "region": {"left": left, "top": 0, "right": left + 3, "bottom": 3}}}
                    for left in (0, 4)
                ]
            }
        )
        self.evidence = list(self.session.state["measurements"])
        self.session.on_prepared([self.session.context()])
        sketch = next(identity for identity, entry in self.session.store.material_entries().items() if entry["kind"] == "sketch")
        self.session._submit_integration({"preserve": True, "review_ids": [sketch]})
        self.session.group_accepted = False
        self.session.selected_evidence, self.session.presented_evidence = [], set()
        pending = self.session.comparisons.pending[0]
        self.session._read_library(
            {
                "work_id": pending["work_id"],
                "requests": [{"library": "sketch_library", "ids": [sketch]}],
            }
        )

    def test_exhausted_measurements_remain_readable_and_both_crops_reach_next_request(self) -> None:
        expected_images = {png_data_url(Path(record.artifact_path)) for record in self.session.state["measurements"].values()}
        self.assertEqual(len(expected_images), 2)
        original_count = len(self.session.measurements.records)

        def respond(request: dict[str, object]) -> dict[str, object]:
            exposed = {item["function"]["name"] for item in request["tools"]}
            self.assertNotIn("measure_reference", exposed)
            self.assertIn("read_measurements", exposed)
            if not tool_results(request):
                return self.call("read_measurements", {"evidence_ids": self.evidence})
            blocks = [block for message in request["messages"] if isinstance(message["content"], list) for block in message["content"]]
            image_urls = {block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"}
            measurements = [json.loads(block["text"])["measurement"] for block in blocks if block.get("text", "").startswith('{"measurement":')]
            self.assertTrue(expected_images <= image_urls)
            self.assertEqual({measurement["id"] for measurement in measurements}, set(self.evidence))
            self.assertEqual(self.session.presented_evidence, set(self.evidence))
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        self.session._run_phase(configure_analysis_model(build_model(), self.session.options), initial=False)
        self.assertTrue(self.session.group_accepted)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.session.measurements.records), original_count)
        self.assertEqual(list(self.session.state["measurements"]), self.evidence)

    def test_invalid_identity_rejects_entire_batch_without_partial_selection(self) -> None:
        tool = self.session._read_measurements_tool()
        tool.invoke({"evidence_ids": [self.evidence[0]]})
        before = list(self.session.selected_evidence)
        with self.assertRaisesRegex(ValueError, "existing measurement identities"):
            tool.invoke({"evidence_ids": [self.evidence[1], "unknown-evidence"]})
        self.assertEqual(self.session.selected_evidence, before)
        self.assertEqual(list(self.session.state["measurements"]), self.evidence)
        self.assertFalse(self.session.presented_evidence)
