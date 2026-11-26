"""覆盖诊断制品、参数白名单和门禁 B 修订材料."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from PIL import Image

from shader_deep.domain.review_items import CheckAnswer, CheckFinding
from shader_deep.domain.scene_planning import ExtraCandidate
from shader_deep.domain.shader_parameters import ParameterSpec
from shader_deep.domain.tasks import CandidateRecord
from shader_deep.domain.visual_review import EvidenceGap, PixelRegion, RequiredRegion, ReviewCoverage
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters
from shader_deep.infrastructure.storage.planning_package import read_planning_package, write_planning_package
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.diagnosis import FindingDiagnosis, run_diagnosis
from shader_deep.workflows.numeric_revision import NumericRevision
from shader_deep.workflows.optimization_workflow import GateBDecision, read_workflow
from shader_deep.workflows.optimization_workflow.candidates import _candidate, _review_findings
from shader_deep.workflows.optimization_workflow.contracts import WorkflowState
from shader_deep.workflows.optimization_workflow.entry import _structure
from shader_deep.workflows.optimization_workflow.revisions import _auto_numeric
from shader_deep.workflows.optimization_workflow.stages import _Context
from shader_deep.workflows.optimization_workflow.storage import _fingerprint
from shader_deep.workflows.review import freeze_workflow_review, run_workflow_review
from shader_deep.workflows.review.options import ReviewOptions
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context
from tests.unit_tests.workflows.test_upstream_review_criteria import scene_task

if TYPE_CHECKING:
    from shader_deep.infrastructure.storage.review_package import ReviewPackage
    from shader_deep.workflows.diagnosis import RevisionRoute
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate

_FRONT = PixelRegion(left=1, top=1, right=5, bottom=6)
_SAMPLE = PixelRegion(left=7, top=2, right=13, bottom=9)


class DiagnosisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.plan = read_planning_package(planning_report(self.root / "fixture"))
        self.reference = self.plan.source.reference_path
        self.render = self.root / "render"
        self.render.mkdir()
        self.code, self.preview, self.parameters = self.render / "shader.glsl", self.render / "preview.png", self.render / "parameters.json"
        self.code.write_text("uniform float u_gain;\nvoid mainImage(out vec4 c, in vec2 p) { c = vec4(u_gain); }", encoding="utf-8")
        with Image.new("RGB", (32, 24), (225, 130, 110)) as image:
            image.save(self.preview)

    def candidate(self, target: str | None) -> CandidateRecord:
        parameter = ParameterSpec(name="u_gain", type="float", min=0.0, max=1.0, default=0.5, target_ref=target)
        CandidateParameters(specs=(parameter,), uniforms={"u_gain": 0.5}).save(self.parameters, self.code, self.preview, time=0)
        return CandidateRecord(
            id="C1", task_id="G1", code_path=str(self.code), preview_path=str(self.preview), render_record_path=str(self.parameters)
        )

    def review(self, source: str = "F8", *, protect_relation: bool = False, gap: EvidenceGap | None = None) -> tuple[ReviewPackage, ReviewModel]:
        request = scene_task(source=source)
        canvas = PixelRegion(left=0, top=0, right=32, bottom=24)
        rows = [RequiredRegion(dimension="color", check="保留前圆填充", region=_FRONT)]
        if protect_relation:
            rows.append(RequiredRegion(dimension="relations", check="核对交叠变化", region=_SAMPLE))
        coverage = ReviewCoverage(reference_window=canvas, rendered_window=canvas, required_regions=tuple(rows))
        criteria = freeze_workflow_review(request.model_copy(update={"coverage": coverage}))
        finding = CheckFinding(region="后圆", reference_observation="青", rendered_observation="红", severity="major", impact="偏离目标主色")
        first, *matched = criteria.checks
        answer = (
            CheckAnswer(check_id=first.check_id, rationale="缺少局部测量", unassessed_reason="需放大局部", evidence_gap=gap)
            if gap is not None
            else CheckAnswer(check_id=first.check_id, rationale="目标颜色明显不同", findings=(finding,))
        )
        answers = (answer, *(CheckAnswer(check_id=item.check_id, rationale="表现一致") for item in matched))
        model = ReviewModel(
            responses=[review_call("finish_review", {"summary": "双图对照", "answers": [item.model_dump(mode="json") for item in answers]})]
        )
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            result = run_workflow_review(self.reference, self.preview, criteria, options=ReviewOptions(output_dir=self.root / "review"))
        self.assertEqual(result.status, "completed", result.error)
        return read_review_package(result.report_dir), model

    def diagnosis(self, review: ReviewPackage, route: RevisionRoute, source: str = "F8", names: tuple[str, ...] = ()) -> NativeRoutingModel:
        (issue,) = review.report.submission.issues
        item = FindingDiagnosis(
            issue_id=issue.id, source_ref=source, route=route, target="调整后圆色场", evidence=("对照后圆的可见主色",), parameter_names=names
        )
        return NativeRoutingModel(responses=[call("DiagnosisSubmission", {"items": [item.model_dump(mode="json")]})])

    def workflow(self, report: ReviewPackage, record: CandidateRecord) -> tuple[_Context, WorkflowCandidate]:
        criteria = report.source.workflow_criteria
        if criteria is None:
            self.fail("评审包缺少冻结的门禁标准")
        write_snapshot(self.root / "criteria.json", criteria.model_dump(mode="json"))
        context = _Context(
            WorkflowState(
                directory=str(self.root),
                phase="gate_b",
                reference_path=self.reference.relative_to(self.root).as_posix(),
                reference_sha256=_fingerprint(self.reference),
                request=report.source.request.requirements,
                width=32,
                height=24,
                remaining_model_calls=10,
                planning_path=self.plan.directory.relative_to(self.root).as_posix(),
                observation_path=self.plan.source.observation_dir.relative_to(self.root).as_posix(),
                frozen_criteria_path="criteria.json",
                frozen_criteria_sha256=criteria.criteria_sha256,
                sketch_id="S1",
            )
        )
        candidate = _candidate(context, record, self.render, None, time=0, probe=None)
        candidate = _review_findings(context, candidate, report)
        context.update(current_candidate=candidate, candidates=(candidate,))
        return context, candidate

    def test_diagnosis_model_can_read_inactive_mechanism_and_extra_candidate(self) -> None:
        extra = ExtraCandidate(target="F8", mechanisms=("M3",), reason="试验底色柔化")
        plan_dir = write_planning_package(
            self.root / "alternate", self.plan.source, self.plan.report.model_copy(update={"extra_candidates": (extra,)})
        )
        review, _ = self.review()
        model = self.diagnosis(review, "planning_limit")
        proposal = run_diagnosis(review.directory, plan_dir, sketch_id="S1", alternative_index=0, candidate=self.candidate("F8"), model=model)
        self.assertEqual(proposal.items[0].route, "planning_limit")
        payload = json.loads(str(model.requests[0][-1].content))
        active = {item["id"] for item in payload["planning"]["mechanisms"]}
        available = {item["id"]: item for item in payload["available_mechanisms"]}
        self.assertNotIn("M3", active)
        self.assertIn("多点求值", available["M3"]["method"])
        self.assertEqual(available["M3"]["conditions"], ["底层颜色可独立重复求值"])
        self.assertEqual(payload["extra_candidates"], [extra.model_dump(mode="json")])

    def test_numeric_diagnosis_accepts_direct_ownership_and_explicitly_named_global_parameters(self) -> None:
        for source, targets in (("F8", ("F8", "E2", None)), ("E2", ("F8",)), ("user_requirement", ("F8", None))):
            review, _ = self.review(source)
            for target in targets:
                with self.subTest(source=source, target=target):
                    model = self.diagnosis(review, "numeric_tuning", source, ("u_gain",))
                    proposal = run_diagnosis(review.directory, self.plan.directory, sketch_id="S1", candidate=self.candidate(target), model=model)
                    self.assertEqual(proposal.items[0].parameter_names, ("u_gain",))

    def test_numeric_diagnosis_rejects_unrelated_sibling_and_unknown_parameter(self) -> None:
        review, _ = self.review()
        for target, names in (("F3", ("u_gain",)), ("E7", ("u_gain",)), (None, ("u_unknown",)), ("F8", ("u_gain", "u_gain"))):
            with self.subTest(target=target, names=names), self.assertRaisesRegex(ValueError, "外置参数"):
                model = self.diagnosis(review, "numeric_tuning", names=names)
                run_diagnosis(review.directory, self.plan.directory, sketch_id="S1", candidate=self.candidate(target), model=model)

    def test_structure_passes_manual_note_diagnostic_target_and_protection_to_generation(self) -> None:
        review, _ = self.review()
        record = self.candidate("F8")
        context, candidate = self.workflow(review, record)
        proposal = run_diagnosis(
            review.directory, self.plan.directory, sketch_id="S1", candidate=record, model=self.diagnosis(review, "code_structure")
        )
        choice = GateBDecision(action="reroute", route="code_structure", reason="只改后圆公式")
        with patch("shader_deep.workflows.optimization_workflow.entry._generate", return_value=None) as generate:
            _structure(context, choice, candidate, proposal)
        guidance = generate.call_args.kwargs["guidance"]
        for text in ("只改后圆公式", "调整后圆色场", "保留前圆填充", "核对交叠变化", "(1, 1, 5, 6)"):
            self.assertIn(text, guidance)

    def test_automatic_numeric_exposes_unprotected_global_check(self) -> None:
        review, _ = self.review()
        record = self.candidate("F8")
        context, candidate = self.workflow(review, record)
        proposal = run_diagnosis(
            review.directory, self.plan.directory, sketch_id="S1", candidate=record, model=self.diagnosis(review, "numeric_tuning", names=("u_gain",))
        )
        with patch("shader_deep.workflows.optimization_workflow.revisions.run_numeric_revision") as optimize:
            self.assertIsNone(_auto_numeric(context, candidate, proposal))
        optimize.assert_not_called()
        state = read_workflow(self.root)
        self.assertIn("核对交叠变化", state.reason or "")
        self.assertNotIn("保留前圆填充", state.reason or "")
        self.assertEqual(state.current_candidate, candidate)

    def test_satisfied_numeric_baseline_keeps_visual_verdict_and_no_failed_attempt(self) -> None:
        review, _ = self.review(protect_relation=True)
        record = self.candidate("F8")
        context, candidate = self.workflow(review, record)
        proposal = run_diagnosis(
            review.directory, self.plan.directory, sketch_id="S1", candidate=record, model=self.diagnosis(review, "numeric_tuning", names=("u_gain",))
        )
        result = NumericRevision(run_dir=self.root, candidate=None, baked_code_path=None, diagnostics={"status": "baseline_satisfied"})
        with patch("shader_deep.workflows.optimization_workflow.revisions.run_numeric_revision", return_value=result) as optimize:
            self.assertIsNone(_auto_numeric(context, candidate, proposal))
        optimize.assert_called_once()
        state = read_workflow(self.root)
        self.assertEqual(state.optimization, result.diagnostics)
        self.assertEqual(state.attempts, ())
        self.assertEqual(state.escalations, ())
        self.assertEqual(state.current_candidate, candidate)

    def test_first_review_request_and_report_preserve_sample_recoverable_gap(self) -> None:
        gap = EvidenceGap(kind="insufficient_crop_or_sample", sample_region=_SAMPLE)
        report, model = self.review(gap=gap)
        payload = review_context(model.requests[0])
        self.assertNotIn("final_call", payload)
        notes = str(payload["evidence_gap_notes"])
        self.assertIn("insufficient_crop_or_sample", notes)
        self.assertIn("missing_context_or_frame", notes)
        self.assertEqual(tuple(report.evidence_gaps.values()), (gap,))
