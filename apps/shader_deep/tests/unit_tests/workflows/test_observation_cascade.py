"""验证观察复核的可改范围及同一 PNG 上一次有实质变化的补采."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.domain.evidence import ImageRegion
from shader_deep.domain.review_items import CheckAnswer
from shader_deep.domain.scene_observations import ReferenceMeasurement
from shader_deep.domain.scene_planning import ObservationRevisionRequest, RiskProbe
from shader_deep.domain.visual_review import EvidenceGap, PixelRegion
from shader_deep.infrastructure.storage.observation_package import write_observation_package
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.diagnosis import DiagnosisSubmission, FindingDiagnosis
from shader_deep.workflows.optimization_workflow import GateADecision, GateBDecision, advance_workflow, start_workflow
from shader_deep.workflows.optimization_workflow.probes import _local_standard
from shader_deep.workflows.planning.cascade import PlanningRevision, observation_impact
from shader_deep.workflows.planning.entry import run_planning
from shader_deep.workflows.planning.review_criteria import (
    SceneCriteriaDraft,
    _merge_criteria,
    derive_local_review_request,
    draft_scene_review_criteria,
)
from shader_deep.workflows.review import freeze_workflow_review
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.workflows import test_optimization_workflow as workflow_fixture
from tests.unit_tests.workflows.test_upstream_review_criteria import scene_task

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import ReviewRequest
    from shader_deep.workflows.planning.cascade import ObservationImpact
    from shader_deep.workflows.review.session import ReviewSession


class ObservationCascadeTests(unittest.TestCase):
    def test_feature_change_keeps_other_elements_choices_and_human_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = read_planning_package(planning_report(Path(directory)))
        manifest = package.source.observation_manifest
        scene = package.source.observations
        features = tuple(item.model_copy(update={"description": "有方向的冷色渐变"}) if item.id == "F8" else item for item in scene.features)
        impacted = observation_impact((manifest, scene), (manifest, scene.model_copy(update={"features": features}))).planning
        self.assertEqual(set(impacted), {"E2", "F8", "R2", "R9"})
        revision = PlanningRevision(package.report, impacted)
        base = package.report.sketches[0]
        for target, allowed in (("F8", True), ("F15", False)):
            sketch = base.model_copy(update={"default": {**base.default, target: ("M3",)}})
            changed = package.report.model_copy(update={"sketches": (sketch,)})
            if allowed:
                revision.validate(changed)
            else:
                with self.assertRaisesRegex(ValueError, "无关目标"):
                    revision.validate(changed)
        request = scene_task()
        dims = tuple(
            dim.model_copy(update={"required_checks": ("核对后圆冷色", "尝试改写前圆")}) if dim.id == "color" else dim for dim in request.dimensions
        )
        merged = _merge_criteria(request, SceneCriteriaDraft(dimensions=dims), set(impacted), 32, 24)
        self.assertEqual(next(row for row in merged.dimensions if row.id == "color").required_checks, ("保留前圆填充", "核对后圆冷色"))

        target = RiskProbe(target_ids=("F15",), reason="核对前圆", context_mode="isolated_solid")
        canvas = PixelRegion(left=0, top=0, right=32, bottom=24)

        def standard(task: ReviewRequest) -> dict[str, object]:
            scene_binding = freeze_workflow_review(task)
            local = derive_local_review_request(scene_binding, target, scene, canvas, canvas=canvas)
            return _local_standard(freeze_workflow_review(local), scene_binding)

        self.assertEqual(standard(request), standard(merged))

    def test_notes_promoted_gap_and_new_anchor_update_only_their_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = read_planning_package(planning_report(Path(directory))).source
        manifest, scene = source.observation_manifest, source.observations

        def impact(**fields: object) -> ObservationImpact:
            return observation_impact((manifest, scene), (manifest.model_copy(update=fields), scene))

        notes = impact(context_notes=("不同全图上下文",))
        self.assertEqual(set(notes.planning), scene.identities)
        self.assertEqual(set(notes.criteria), scene.identities | {"user_requirement"})
        elevated = impact(gaps=manifest.open_questions, open_questions=())
        self.assertEqual((elevated.planning, elevated.criteria), (("R9",), ()))
        area = ImageRegion(left=3, top=3, right=7, bottom=7)
        anchor = ReferenceMeasurement(
            id="m1", kind="region_stats", region=area, question="前圆亮度", image_sha256=manifest.reference.sha256, mean_rgb=(1, 1, 1), mean_luma=1
        )
        added = manifest.model_copy(update={"reference_measurements": (anchor,)})
        request = ObservationRevisionRequest(
            disputed_ids=("F15",), contradiction="亮度", suggested_region=PixelRegion(left=2, top=2, right=10, bottom=10)
        )
        scoped = observation_impact((manifest, scene), (added, scene), request)
        self.assertEqual(set(scoped.planning), {"F15", "E7", "R9"})
        self.assertFalse(scoped.criteria)
        unscoped = observation_impact((manifest, scene), (added, scene))
        self.assertEqual(set(unscoped.planning), scene.identities)
        renamed = anchor.model_copy(update={"id": "another-run-e001", "question": "不同的提问"})
        same_pixels = added.model_copy(update={"reference_measurements": (renamed,)})
        stable = observation_impact((added, scene), (same_pixels, scene), request)
        self.assertFalse(stable.planning)
        self.assertFalse(stable.changed_metadata)

    def test_observation_reroute_seeds_the_real_planner_and_keeps_unrelated_approved_checks(self) -> None:
        setup = workflow_fixture.OptimizationWorkflowTests()
        self.addCleanup(setup.doCleanups)
        setup.setUp()
        old = setup.generate_one()
        directory = Path(old.directory)
        source = read_planning_package(directory / old.planning_path).source
        observations = source.observations
        changed = tuple(row.model_copy(update={"description": "后圆冷色渐变"}) if row.id == "F8" else row for row in observations.features)
        setup.observation = write_observation_package(
            setup.root / "revised", observations.model_copy(update={"features": changed}), source.observation_manifest, source.reference_bytes
        )
        planner = NativeRoutingModel(responses=[call("read_file", {"file_path": "/draft/planning.json", "limit": 2000}), call("finish_planning", {})])
        draft = scene_task()
        dims = [
            row.model_copy(update={"required_checks": ("核对后圆冷色", "不能替换前圆")}).model_dump(mode="json")
            if row.id == "color"
            else row.model_dump(mode="json")
            for row in draft.dimensions
        ]
        organizer = NativeRoutingModel(responses=[call("SceneCriteriaDraft", {"dimensions": dims})])
        with (
            patch(
                "shader_deep.workflows.optimization_workflow.stages.run_planning",
                side_effect=lambda *args, **kwargs: run_planning(*args, **{**kwargs, "model": planner}),
            ),
            patch(
                "shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria",
                side_effect=lambda *args, **kwargs: draft_scene_review_criteria(*args, **{**kwargs, "model": organizer}),
            ),
        ):
            state = advance_workflow(directory, GateBDecision(action="reroute", route="observation_error", reason="核实后圆", disputed_ids=("F8",)))
        self.assertEqual(state.phase, "gate_a", state.reason)
        self.assertEqual(state.changed_observation_ids, ("F8",))
        self.assertEqual(set(state.observation_cascades[-1].affected_sources), {"F8", "E2", "R2", "R9"})
        self.assertEqual(read_planning_package(directory / state.planning_path).source.observations.features[0].description, "后圆冷色渐变")
        self.assertEqual(len(planner.requests), 2)
        self.assertIn("保留前圆填充", (directory / state.criteria_path).read_text())
        self.assertNotIn("不能替换前圆", (directory / state.criteria_path).read_text())

    def test_gate_review_collects_missing_pixels_once_and_sends_context_gaps_to_the_human(self) -> None:
        setup = workflow_fixture.OptimizationWorkflowTests()
        self.addCleanup(setup.doCleanups)
        setup.setUp()
        region = PixelRegion(left=2, top=2, right=7, bottom=6)
        for gap in (EvidenceGap(kind="insufficient_crop_or_sample", sample_region=region), EvidenceGap(kind="missing_context_or_frame")):
            with self.subTest(kind=gap.kind):
                self._check_review_gap(setup, gap, region)

    def _check_review_gap(self, setup: workflow_fixture.OptimizationWorkflowTests, gap: EvidenceGap, region: PixelRegion) -> None:
        evidence = []

        def assess(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            evidence.append(session.source.evidence)
            answers = [CheckAnswer(check_id=item.check_id, rationale="观察记录") for item in session.checks]
            if len(evidence) == 1:
                answers[0] = answers[0].model_copy(update={"unassessed_reason": "需要额外证据", "evidence_gap": gap})
            session.finish("按项核查", answers=tuple(answers))

        def diagnose(data: dict, _model: object) -> DiagnosisSubmission:
            items = [
                FindingDiagnosis(issue_id=identity, source_ref=source, route="code_structure", target="更改结构", evidence=("测试替身建议",))
                for identity, source in data["issues_to_diagnose"].items()
            ]
            return DiagnosisSubmission(items=tuple(items))

        start = start_workflow(setup.reference, "复现参考图", output_dir=setup.root / "runs")
        directory = Path(start.directory)
        with (
            patch("shader_deep.workflows.review.entry._invoke", side_effect=assess),
            patch("shader_deep.workflows.diagnosis._request", side_effect=diagnose),
        ):
            state = advance_workflow(directory, GateADecision(action="generate_scene", sketch_id="S1"))
        candidate = state.current_candidate
        self.assertEqual(state.phase, "gate_b", state.reason)
        package = read_review_package(directory / candidate.review_path)
        if gap.kind == "insufficient_crop_or_sample":
            self.assertEqual(len(evidence), 2)
            sample = package.report.evidence.samples[0]
            self.assertEqual((sample.reference.region, sample.rendered.region, sample.reference.pixel_count), (region, region, 20))
            self.assertEqual(candidate.verdict, "no_major_issue_observed")
        else:
            self.assertEqual(len(evidence), 1)
            self.assertEqual(list(candidate.evidence_gaps.values()), ["missing_context_or_frame"])
            self.assertEqual(list(package.report.evidence_gaps.values()), [gap])
            card = json.loads((directory / candidate.diagnosis_path).read_bytes())
            self.assertEqual(card["items"][0]["route"], "evidence_gap")
