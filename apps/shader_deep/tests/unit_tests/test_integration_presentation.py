"""验证裁剪完整呈现和并行探索中断后的真实终态收集."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import as_completed
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image

from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.exploration_worker import ExplorationOutcome, OutlineIssue
from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions
from shader_deep.blackboard import add_target, add_task, new_blackboard
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TargetRecord, TaskRecord
from tests.unit_tests.test_possibility_library import outline, report


class IntegrationPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        path = self.directory / "reference.png"
        with Image.new("RGB", (8, 6), "white") as image:
            image.save(path)
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="test", reference_path=str(path)))
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="test"))
        self.session = ExplorationSession(state, "A1", AnalysisOptions(max_parallel=2), self.directory, png_data_url(path))
        self.session._submit_outline(outline(), ["平面组织", "空间形态"])

    def prepare_package(self) -> None:
        for task in self.session._register():
            self.session._commit_report(task, report())
        targets = [item.id for item in self.session.store.library.feature_library]
        self.session._read_library(
            {
                "comparison": {"question": "比较拥有者", "target_ids": targets},
                "requests": [{"library": "feature_library", "ids": targets}],
            }
        )

    def measure(self, regions: list[dict[str, int]]) -> None:
        self.session._measurement_tool().invoke(
            {"requests": [{"question": "检查局部", "measurement": {"kind": "crop", "region": region}} for region in regions]}
        )

    def test_missing_measurement_text_or_image_cannot_authorize_submission(self) -> None:
        self.prepare_package()
        self.measure([{"left": 0, "top": 0, "right": 3, "bottom": 3}])
        message = self.session.context()
        for removed in (-1, -2):
            with self.subTest(removed_block=removed):
                self.session.presented_evidence.clear()
                blocks = list(message.content)
                blocks.pop(removed)
                self.session.on_prepared([message.model_copy(update={"content": blocks})])
                self.assertFalse(self.session.presented_evidence)
                with self.assertRaisesRegex(ValueError, "Receive all requested measurements"):
                    self.session._submit_integration({"preserve": True})
        self.session.on_prepared([message])
        revision = self.session.store.revision
        receipt = self.session._submit_integration({"preserve": True})
        self.assertEqual(json.loads(receipt)["status"], "accepted")
        self.assertEqual(self.session._submit_integration({"preserve": True}), receipt)
        self.assertEqual(self.session.store.revision, revision + 1)

    def test_partial_measurement_batch_does_not_hide_the_missing_crop(self) -> None:
        self.prepare_package()
        self.measure([{"left": 0, "top": 0, "right": 3, "bottom": 3}, {"left": 4, "top": 1, "right": 8, "bottom": 5}])
        message = self.session.context()
        blocks = list(message.content)
        blocks.pop()
        self.session.on_prepared([message.model_copy(update={"content": blocks})])
        self.assertEqual(self.session.presented_evidence, {self.session.selected_evidence[0]})
        with self.assertRaisesRegex(ValueError, "Receive all requested measurements"):
            self.session._submit_integration({"preserve": True})
        self.session.on_prepared([message])
        self.assertEqual(self.session.presented_evidence, set(self.session.selected_evidence))
        self.assertEqual(json.loads(self.session._submit_integration({"preserve": True}))["status"], "accepted")

    def test_interrupt_collects_completed_report_and_inflight_issue(self) -> None:
        committed, second_started = Event(), Event()
        calls = 0

        def worker(*_arguments: object, **keywords: object) -> ExplorationOutcome:
            if str(keywords["task_id"]).endswith("a001"):
                keywords["commit_report"](report())
                committed.set()
                return ExplorationOutcome(
                    report=report(),
                    execution=AnalysisExecution(status="completed"),
                    issues=(OutlineIssue(region="顶部", description="需要复查背景"),),
                    error=None,
                )
            second_started.set()
            self.assertTrue(self.session.cancelled.wait(timeout=5))
            return ExplorationOutcome(
                report=None,
                execution=AnalysisExecution(status="stopped", error="cancelled"),
                issues=(OutlineIssue(region="底部", description="无法确定遗漏对象"),),
                error="cancelled",
            )

        def interrupted_once(futures: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertTrue(committed.wait(timeout=5))
                self.assertTrue(second_started.wait(timeout=5))
                raise KeyboardInterrupt
            return as_completed(futures)

        with (
            patch("shader_deep.analysis.exploration_session.run_exploration", worker),
            patch("shader_deep.analysis.exploration_session.as_completed", interrupted_once),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.session._run_batch()
        self.assertEqual(self.session.stop_reason, "interrupted")
        self.assertEqual(sorted(worker.status for worker in self.session.workers.values()), ["completed", "stopped"])
        self.assertEqual(len(self.session.store.report_ids), 1)
        self.assertEqual({issue["region"] for issue in self.session.issues}, {"顶部", "底部"})
        self.session.finalize()
        self.assertEqual(self.session.summary_result.status, "partial")
        snapshot = json.loads((self.directory / "run.json").read_text())
        self.assertEqual(sorted(worker["status"] for worker in snapshot["worker_executions"].values()), ["completed", "stopped"])


if __name__ == "__main__":
    unittest.main()
