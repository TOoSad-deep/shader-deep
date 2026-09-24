"""验证退出和模型异常仍交付已发布库, 并保留未发布整合草稿."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from pydantic import TypeAdapter

from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.exploration import ExplorationReport
from shader_deep.analysis.exploration_worker import ExplorationOutcome
from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions
from tests.unit_tests._generation_fixture import GenerationFixture

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.analysis.loop import AnalysisLoop


OUTLINE = {
    "outline": {"elements": [{"id": "E1", "name": "背景", "scope": "全图", "salient_features": ["均匀颜色"]}], "relations": []},
    "directions": ["平面方向", "空间方向"],
}
REPORT = TypeAdapter(ExplorationReport).validate_python(
    {
        "sketch_library": [{"id": "S1", "name": "组成", "element_ids": ["E1"], "composition": ["均匀颜色"]}],
        "feature_library": [],
        "relation_library": [],
    }
)


class IntegrationInterruptTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.analysis_options = AnalysisOptions(output_dir=self.root / "interruption", max_parallel=2, max_request_retries=0)
        self.failure: BaseException = RuntimeError("fixture model failed")

    def _run_loop(self, loop: AnalysisLoop, *_arguments: object) -> None:
        if loop.submission_handler.tool.name == "submit_visual_outline":
            reply = loop.submission_handler.handle("submit_visual_outline", OUTLINE)
            self.assertIsNone(reply.category)
            return
        if loop.submission_handler.tool.name == "plan_comparisons":
            context = loop.context()
            loop.on_prepared([context])
            payload = json.loads(context.content[0]["text"])
            identities = [entry["handle"] for entry in payload["entries"] if entry["kind"] == "sketch"]
            reply = loop.submission_handler.handle(
                "plan_comparisons", {"comparisons": [{"kind": "sketch", "target_ids": identities, "question": "比较组成"}], "reason": "同范围组成"}
            )
            self.assertIsNone(reply.category)
            return
        # 有效入库之后留下无效决定草稿, 随后中断本包, 不能发布这次决定.
        reply = loop.submission_handler.handle("submit_integration", {"merges": [{"kind": "sketch", "members": ["missing"]}]})
        self.assertIsNotNone(reply.category)
        raise self.failure

    @staticmethod
    def _worker(*_arguments: object, **kwargs: object) -> ExplorationOutcome:
        kwargs["commit_report"](REPORT)
        return ExplorationOutcome(report=REPORT, execution=AnalysisExecution(status="completed"), issues=(), error=None)

    def _assert_saved_partial(self, directory: Path, stop_reason: str) -> None:
        snapshot = json.loads((directory / "run.json").read_text())
        self.assertEqual(snapshot["stop_reason"], stop_reason)
        self.assertNotEqual(snapshot["main_execution"]["status"], "running")
        child = json.loads((directory / "integration-subagent.json").read_text())
        self.assertEqual(child["stop_reason"], stop_reason)
        self.assertEqual(child["execution"], snapshot["integration_execution"])
        self.assertIn(child["execution"]["status"], {"stopped", "failed"})
        self.assertTrue(child["execution"]["error"])
        identity = snapshot["summary_result_id"]
        self.assertIsNotNone(identity)
        result = snapshot["blackboard"]["results"][identity]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["analysis_detail"]["sketch_library"]), 2)
        published = sorted((directory / "possibility_library").glob("version-*.json"))
        latest = json.loads(published[-1].read_text())
        self.assertEqual(result["analysis_detail"], latest["library"])
        drafts = [json.loads(path.read_text()) for path in (directory / "submissions").glob("*.json")]
        pending = [draft for draft in drafts if draft["tool_name"] == "submit_integration"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["errors"])
        self.assertNotEqual(pending[0].get("status"), "submitted")
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "run_finished")

    def test_keyboard_interrupt_reraises_after_saving_partial_library_and_draft(self) -> None:
        self.failure = KeyboardInterrupt()
        with (
            patch("shader_deep.analysis.exploration_session.AnalysisLoop.run", autospec=True, side_effect=self._run_loop),
            patch("shader_deep.analysis.exploration_session.run_exploration", side_effect=self._worker),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_analysis(self.root / "reference.PNG", "分析颜色", options=self.analysis_options)
        directories = list(self.analysis_options.output_dir.iterdir())
        self.assertEqual(len(directories), 1)
        self._assert_saved_partial(directories[0], "interrupted")
        self.assertEqual(self.requests, [])

    def test_model_exception_returns_partial_library_and_preserves_failure(self) -> None:
        with (
            patch("shader_deep.analysis.exploration_session.AnalysisLoop.run", autospec=True, side_effect=self._run_loop),
            patch("shader_deep.analysis.exploration_session.run_exploration", side_effect=self._worker),
        ):
            outcome = run_analysis(self.root / "reference.PNG", "分析颜色", options=self.analysis_options)
        self._assert_saved_partial(outcome.run_dir, "error")
        self.assertEqual(outcome.summary_result.status, "partial")
        snapshot = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertIn("fixture model failed", snapshot["main_execution"]["error"])
        self.assertEqual(self.requests, [])
