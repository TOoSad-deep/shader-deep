"""两项比较的固定输入、独立历史及失败和取消边界."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.agents.generation.agent import _execute
from shader_deep.domain.generation_comparison import GenerationPlan, select_comparison_item
from shader_deep.workflows.generation_comparison import run_generation_comparison
from tests.unit_tests.agents.test_context import BASE_CODE
from tests.unit_tests.agents.test_generation import ThreadBoundRenderer
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, generation_report, task_payloads

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from shader_deep.agents.generation.tools import RenderSession
    from shader_deep.domain.generation_comparison import GenerationComparison


class GenerationComparisonTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.report = generation_report(self.root)
        self.plans = (GenerationPlan(name="默认遮罩", sketch_id="S1"), GenerationPlan(name="高斯备选", sketch_id="S1", alternative=0))
        ThreadBoundRenderer.renders = []
        ThreadBoundRenderer.closed = 0
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)

    def compare(self) -> GenerationComparison:
        return run_generation_comparison(
            self.report,
            self.plans,
            "比较圆形的两种构造",
            background="透明",
            state=self.state,
            baseline_id="B7",
            asset_root=self.root,
            output_dir=self.options.output_dir,
        )

    def test_two_plans_share_fixed_inputs_and_model_but_not_candidate_history(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                for path in (self.report / "reference.png", self.root / "baseline.glsl", self.root / "baseline.png"):
                    path.unlink()
                os.environ["MICU_MODEL"] = "changed-after-first-request"
            return self.render_then_finish(request)

        self.response = respond
        comparison = self.compare()
        self.assertEqual([item.stop_reason for item in comparison.items], ["completed", "completed"])
        self.assertIsNone(comparison.selected_item)
        self.assertEqual(ThreadBoundRenderer.renders, [(32, 24, 0.0), (32, 24, 0.0)])
        self.assertEqual({request["model"] for request in self.requests}, {comparison.model_name})
        self.assertEqual(comparison.model_name, "fixture-model")
        self.assertEqual(comparison.baseline_id, "B7")
        self.assertEqual(len({item.run_dir for item in comparison.items}), 2)
        for item in comparison.items:
            record = json.loads((Path(item.run_dir) / "run.json").read_text())
            binding = record["blackboard"]["tasks"][item.task_id]["generation_binding"]
            self.assertEqual(binding["content_sha256"], comparison.report_sha256)
            self.assertEqual(binding["reference_sha256"], comparison.reference_sha256)
            self.assertEqual(Path(record["inputs"]["baseline"]["code_path"]).read_text(), BASE_CODE)
        second_requests = [
            request for request in self.requests if task_payloads(request)[0]["generation_scheme"]["selected"]["choices"] == {"F1": ["M2"]}
        ]
        self.assertEqual(len(second_requests), 2)
        self.assertEqual([item["id"] for item in task_payloads(second_requests[0])[0]["candidates"]], ["B7"])
        self.assertNotIn(comparison.items[0].selected_candidate_id, json.dumps(second_requests))
        self.assertTrue((Path(comparison.directory) / "comparison.json").is_file())
        self.assertTrue((Path(comparison.directory) / "README.md").is_file())

    def test_one_execution_failure_keeps_artifacts_and_runs_the_other_plan(self) -> None:
        def execute(session: RenderSession, *, model: BaseChatModel | None = None) -> None:
            if session.inputs.binding.alternative is None:
                session.render_shader(BASE_CODE)
                msg = "first model stopped unexpectedly"
                raise RuntimeError(msg)
            _execute(session, model=model)

        with patch("shader_deep.workflows.generation._execute", side_effect=execute):
            comparison = self.compare()
        failed, completed = comparison.items
        self.assertEqual((failed.stop_reason, completed.stop_reason), ("error", "completed"))
        self.assertIn("RuntimeError: first model stopped unexpectedly", failed.error)
        self.assertIsNone(failed.selected_candidate_id)
        self.assertIsNone(failed.preview_path)
        self.assertTrue(list(Path(failed.run_dir).glob("*.glsl")))
        self.assertEqual(Path(completed.code_path).read_text(), BASE_CODE)
        self.assertIsNone(comparison.selected_item)

    def test_cancellation_stops_batch_without_starting_second_execution(self) -> None:
        def cancel(session: RenderSession, **_kwargs: object) -> None:
            session.render_shader(BASE_CODE)
            raise KeyboardInterrupt

        with patch("shader_deep.workflows.generation._execute", side_effect=cancel), self.assertRaises(KeyboardInterrupt):
            self.compare()
        directory = next(self.options.output_dir.iterdir())
        records = [json.loads(path.read_text()) for path in (directory / "runs").glob("*/run.json")]
        self.assertEqual(sorted(record.get("stop_reason", record["phase"]) for record in records), ["error", "prepared"])
        self.assertEqual(ThreadBoundRenderer.renders, [(32, 24, 0.0)])
        self.assertFalse((directory / "comparison.json").exists())
        self.assertEqual(self.requests, [])

    def test_close_failure_keeps_selected_artifact_navigation_and_runs_next_plan(self) -> None:
        close = ThreadBoundRenderer.close

        def fail_first_close(renderer: ThreadBoundRenderer) -> None:
            close(renderer)
            if ThreadBoundRenderer.closed == 1:
                msg = "first renderer close failed"
                raise RuntimeError(msg)

        with patch.object(ThreadBoundRenderer, "close", fail_first_close):
            comparison = self.compare()
        failed, completed = comparison.items
        self.assertEqual((failed.stop_reason, completed.stop_reason), ("error", "completed"))
        self.assertIn("RuntimeError: first renderer close failed", failed.error)
        record = json.loads((Path(failed.run_dir) / "run.json").read_text())
        self.assertEqual(failed.selected_candidate_id, record["selected_candidate_id"])
        self.assertEqual(Path(failed.code_path).read_text(), BASE_CODE)
        self.assertTrue(Path(failed.preview_path).is_file())
        readme = (Path(comparison.directory) / "README.md").read_text()
        self.assertIn(failed.selected_candidate_id, readme)
        self.assertIn(Path(failed.preview_path).name, readme)
        with self.assertRaises(ValueError):
            select_comparison_item(comparison, 0)

    def test_invalid_second_selection_fails_before_any_model_request(self) -> None:
        self.plans = (self.plans[0], GenerationPlan(name="不存在", sketch_id="missing"))
        with self.assertRaises(ValueError):
            self.compare()
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.options.output_dir.iterdir()), [])
