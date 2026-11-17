"""规划适配通过共用磁盘草稿保持字段、版本及只读观察范围."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import ToolMessage

from shader_deep.agents.draft_runtime import DraftReadTracker
from shader_deep.domain.scene_observations import SceneObservationSubmission, build_scene_observations
from shader_deep.domain.scene_planning import ScenePlanningSubmission
from shader_deep.infrastructure.storage.json_draft import DraftError
from shader_deep.infrastructure.storage.planning_draft import PlanningDraftStore


def report() -> ScenePlanningSubmission:
    return ScenePlanningSubmission.model_validate(
        {
            "mechanisms": [{"id": "M1", "name": "圆形遮罩", "method": "由中心半径计算覆盖率"}],
            "sketches": [
                {
                    "id": "S1",
                    "name": "整图圆形",
                    "default": {"F1": ["M1"]},
                    "composition": "在中央应用 [[M1]] 实现 [[F1]]",
                    "alternatives": [{"choices": {"F1": ["M1"]}, "reason": "局部边缘精度调整"}],
                }
            ],
        }
    )


class PlanningDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        observation = SceneObservationSubmission.model_validate(
            {"elements": [{"id": "E1", "name": "圆", "region": "中央", "features": [{"id": "F1", "description": "圆形"}]}]}
        )
        self.observations = build_scene_observations(observation)
        self.store = PlanningDraftStore(self.directory, self.observations)

    def test_create_and_edit_bind_planning_path_and_preserve_omitted_alternative_composition(self) -> None:
        receipt = self.store.create(report())
        self.assertEqual(receipt["path"], "/draft/planning.json")
        self.assertTrue(receipt["validation"]["valid"])
        content, revision = self.store.read()
        self.assertNotIn('"composition": null', content)
        self.assertIsNone(self.store.report().sketches[0].alternatives[0].composition)
        self.store.edit("整图圆形", "单路圆形", expected_revision=revision)
        self.assertEqual(self.store.report().sketches[0].name, "单路圆形")
        self.assertEqual((self.store.history / f"{revision}.json").read_text(), content)
        self.assertFalse((self.directory / "draft" / "observations.json").exists())

    def test_reference_errors_remain_editable_and_diagnostic_path_is_planning_specific(self) -> None:
        payload = report().model_dump(mode="json", exclude_none=True)
        payload["sketches"][0]["default"] = {"F9": ["M1"]}
        receipt = self.store.create(ScenePlanningSubmission.model_validate(payload))
        self.assertFalse(receipt["validation"]["valid"])
        current, revision = self.store.read()
        self.store.edit('"F9"', '"F1"', expected_revision=revision)
        self.assertTrue(self.store.validation(self.store.report())["valid"])
        with self.assertRaises(DraftError) as caught:
            self.store.edit('"M1"', '"M2"', expected_revision=self.store.revision)
        self.assertEqual(caught.exception.path, "/draft/planning.json")
        self.assertNotEqual(self.store.read()[0], current)

    def test_same_revision_pages_must_actually_reach_model_and_edits_require_a_new_read(self) -> None:
        self.store.create(report())
        tracker = DraftReadTracker(self.store, finish_tool="finish_planning")
        text, revision = self.store.read()
        lines = text.splitlines()
        boundary = len(lines) // 2
        first = f"@@ lines 1-{boundary} of {len(lines)} | next offset {boundary} @@\n" + "\n".join(lines[:boundary])
        second = f"@@ lines {boundary + 1}-{len(lines)} of {len(lines)} @@\n" + "\n".join(lines[boundary:])
        tracker.record("first", first)
        tracker.record("second", second)
        self.assertIn("finish_planning", tracker.unseen_message(text, revision))
        tracker.present([ToolMessage(content=first, tool_call_id="first")])
        self.assertIsNone(tracker.seen_revision)
        tracker.present([ToolMessage(content=second + " modified", tool_call_id="second")])
        self.assertIsNone(tracker.seen_revision)
        tracker.present([ToolMessage(content=second, tool_call_id="second")])
        self.assertEqual(tracker.seen_revision, revision)
        self.store.edit("整图圆形", "已修订圆形", expected_revision=revision)
        tracker.invalidate()
        tracker.present([ToolMessage(content=first, tool_call_id="first"), ToolMessage(content=second, tool_call_id="second")])
        self.assertIsNone(tracker.seen_revision)
