"""整图与局部的并行方案对照及固定条件."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.evidence import ImageRegion
from shader_deep.domain.generation_comparison import GenerationPlan
from shader_deep.domain.local_probe import VerifiedSnippet
from shader_deep.domain.review_items import CheckAnswer
from shader_deep.domain.scene_observations import ReferenceMeasurement
from shader_deep.domain.scene_planning import RiskProbe
from shader_deep.domain.tasks import CandidateRecord
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.storage.observation_package import write_observation_package
from shader_deep.infrastructure.storage.planning_inputs import capture_planning_source
from shader_deep.infrastructure.storage.planning_package import read_planning_package, write_planning_package
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.generation_comparison import run_planning_generation_comparison
from shader_deep.workflows.planning.review_criteria import bind_local_probe
from shader_deep.workflows.review import freeze_workflow_review
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.workflows.test_upstream_review_criteria import scene_task

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs
    from shader_deep.workflows.review.session import ReviewSession

_PLANS = (GenerationPlan(name="默认", sketch_id="S1"), GenerationPlan(name="备选", sketch_id="S1", alternative=0))
_WORKFLOW = "shader_deep.workflows.generation_comparison"


class PlanningGenerationComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.planning = planning_report(self.directory)
        self.options = GenerationOptions(width=32, height=24, time=1.5, output_dir=self.directory / "comparisons")

    def plan_with_reference_anchor(self) -> Path:
        plan = read_planning_package(self.planning)
        source = plan.source
        anchor = ReferenceMeasurement(
            id="measured-background",
            kind="region_stats",
            region=ImageRegion(left=1, top=1, right=5, bottom=5),
            question="检查背景颜色",
            image_sha256=source.observation_manifest.reference.sha256,
            mean_rgb=(160.0, 180.0, 210.0),
            mean_luma=178.0,
        )
        manifest = source.observation_manifest.model_copy(update={"reference_measurements": (anchor,)})
        observations = write_observation_package(self.directory / "with-anchor", source.observations, manifest, source.reference_bytes)
        fixed_source = capture_planning_source(observations, self.directory / "fixed", source.request, generation_options=self.options)
        return write_planning_package(self.directory / "anchored-plan", fixed_source, plan.report)

    def snippet(self, planning: Path, target: str) -> VerifiedSnippet:
        package = read_planning_package(planning)
        probe = RiskProbe(target_ids=(target,), reason="验证局部方法", context_mode="isolated_solid")
        binding = bind_local_probe(
            package,
            "S1",
            probe,
            freeze_workflow_review(scene_task()),
            PixelRegion(left=1, top=1, right=7, bottom=7),
            solid_rgba=(0.0, 0.0, 0.0, 1.0),
        )
        return VerifiedSnippet(
            binding=binding,
            glsl_code="void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }",
            uniforms={},
            preview_png=package.source.reference_bytes,
            review_criteria_sha256=binding.criteria.criteria_sha256,
            review_report_sha256="a" * 64,
        )

    def test_parallel_children_use_same_criteria_and_preserve_independent_artifacts(self) -> None:
        start = Barrier(2, timeout=5)
        planning = self.plan_with_reference_anchor()
        snippet = self.snippet(planning, "F8")
        injected = FakeListChatModel(responses=["offline"])

        def generate(
            _state: BlackboardState,
            task_id: str,
            _root: Path,
            options: GenerationOptions,
            *,
            run_dir: Path,
            inputs: CapturedPlanningInputs,
            model: FakeListChatModel,
        ) -> SimpleNamespace:
            self.assertEqual((options.width, options.height, options.time), (32, 24, 1.5))
            self.assertIs(model, injected)
            (captured,) = inputs.verified_snippets
            self.assertEqual(Path(captured.code_path).read_text(encoding="utf-8"), snippet.glsl_code)
            self.assertEqual(Path(captured.preview_path).read_bytes(), snippet.preview_png)
            selected = json.loads(inputs.scheme_json)["selected"]
            start.wait()
            identity = "default" if selected["alternative_index"] is None else "alternative"
            preview, code = run_dir / "candidate.png", run_dir / "candidate.glsl"
            preview.write_bytes(Path(inputs.reference_path).read_bytes())
            code.write_text(identity, encoding="utf-8")
            candidate = CandidateRecord(id=identity, task_id=task_id, preview_path=str(preview), code_path=str(code))
            return SimpleNamespace(selected_candidate=candidate, stop_reason="completed", model_calls=2, baked_code_path=None)

        def review(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            session.finish(
                "固定双图与标准一致",
                answers=tuple(CheckAnswer(check_id=item.check_id, rationale="测试双图一致") for item in session.checks if item.unassessed is None),
            )

        fixed = freeze_workflow_review(scene_task())
        with (
            patch(f"{_WORKFLOW}.build_model") as build_model,
            patch(f"{_WORKFLOW}._run_session", side_effect=generate),
            patch("shader_deep.workflows.review.entry._invoke", side_effect=review),
        ):
            record = run_planning_generation_comparison(
                planning, _PLANS, options=self.options, criteria=fixed, verified_snippets=(snippet,), model=injected
            )
        build_model.assert_not_called()
        self.assertEqual(record.criteria_sha256, fixed.criteria_sha256)
        self.assertEqual([item.selected_candidate_id for item in record.items], ["default", "alternative"])
        self.assertNotEqual(record.items[0].run_dir, record.items[1].run_dir)
        self.assertNotEqual(record.items[0].review_report_dir, record.items[1].review_report_dir)
        for item in record.items:
            self.assertEqual(item.review_status, "completed", item.review_error)
            source = read_review_package(Path(item.review_report_dir)).source
            self.assertEqual(source.workflow_criteria, fixed)
            self.assertEqual(source.request.source.candidate_id, item.selected_candidate_id)
            self.assertEqual([sample.label for sample in source.request.samples], ["参考锚点 measured-background"])
        self.assertEqual(json.loads((Path(record.directory) / "comparison.json").read_text())["criteria_sha256"], fixed.criteria_sha256)

    def test_comparison_rejects_snippet_when_either_plan_uses_a_different_mechanism(self) -> None:
        incompatible = self.snippet(self.planning, "R9")
        with self.assertRaisesRegex(ValueError, "目标方法依赖"):
            run_planning_generation_comparison(self.planning, _PLANS, options=self.options, verified_snippets=(incompatible,))
        self.assertEqual(tuple(self.options.output_dir.iterdir()), ())

    def test_shared_local_context_is_required_and_one_failure_keeps_sibling_result(self) -> None:
        package = read_planning_package(self.planning)
        scene = freeze_workflow_review(scene_task())
        target = PixelRegion(left=0, top=0, right=7, bottom=7)
        probe = RiskProbe(target_ids=("R9",), reason="比较交叠机制", context_mode="proxy_context")
        both = tuple(
            bind_local_probe(package, item.sketch_id, probe, scene, target, alternative_index=item.alternative, proxy_context="底层代理")
            for item in _PLANS
        )
        second = both[1].model_copy(update={"proxy_context": "不同代理"})
        with self.assertRaisesRegex(ValueError, "同一试验目标"):
            run_planning_generation_comparison(self.planning, _PLANS, options=self.options, local_probes=(both[0], second))

        def generate(
            _state: BlackboardState,
            _task_id: str,
            _root: Path,
            _options: GenerationOptions,
            *,
            run_dir: Path,
            inputs: CapturedPlanningInputs,
            model: SimpleNamespace,
        ) -> SimpleNamespace:
            del run_dir, model
            if inputs.local_probe.alternative_index is None:
                msg = "单项生成异常"
                raise RuntimeError(msg)
            return SimpleNamespace(selected_candidate=None, stop_reason="blocked", model_calls=1)

        with (
            patch(f"{_WORKFLOW}.build_model", return_value=SimpleNamespace(model_name="offline")),
            patch(f"{_WORKFLOW}._run_session", side_effect=generate),
        ):
            result = run_planning_generation_comparison(self.planning, _PLANS, options=self.options, local_probes=both)
        self.assertEqual([item.stop_reason for item in result.items], ["error", "blocked"])
        self.assertIn("单项生成异常", result.items[0].error)
