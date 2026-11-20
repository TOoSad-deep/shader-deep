"""从公开入口核对测量、程序未评项和正式报告的完整链路."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import patch

from PIL import Image, ImageDraw

from shader_deep.api import (
    DimensionCriteria,
    PixelRegion,
    RequiredRegion,
    ReviewCoverage,
    ReviewOptions,
    ReviewRequest,
    ReviewSample,
    read_review_package,
    run_review,
)
from shader_deep.domain.visual_review import DIMENSION_IDS, ReviewIssue
from shader_deep.infrastructure.storage.planning_inputs import content_sha256
from tests.unit_tests.fixtures._review_fixture import matched_review, review_criteria
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import DimensionId
    from shader_deep.infrastructure.storage.review_package import ReviewPackage
    from shader_deep.workflows.review.contracts import ReviewOutcome

PICTURES = Path(__file__).resolve().parents[3] / "test_pic"
GEAR = PICTURES / "2d-physics-balls.png"
FROSTED = PICTURES / "supah-frosted-glass.png"
RIM = "完整齿盘外缘"
HOLE = "中心圆孔"


def rect(left: int, top: int, right: int, bottom: int) -> PixelRegion:
    return PixelRegion(left=left, top=top, right=right, bottom=bottom)


def task(kind: Literal["feature", "element"], checks: dict[DimensionId, tuple[str, ...]], **kwargs: object) -> ReviewRequest:
    return ReviewRequest(
        scope="local",
        target_kind=kind,
        target="指定局部",
        requirements="核对指定的可见表现",
        dimensions=tuple(
            DimensionCriteria(id=key, required_checks=checks[key]) if key in checks else DimensionCriteria(id=key, excluded_checks=("不在本次范围",))
            for key in DIMENSION_IDS
        ),
        **kwargs,
    )


def issue_model(request: ReviewRequest, dimension: DimensionId, check: str | None = None) -> ReviewModel:
    report = matched_review(review_criteria(request))
    issue = ReviewIssue(
        id="I1",
        dimension=dimension,
        region="指定区域",
        reference_observation="必要表现存在",
        rendered_observation="必要表现缺失或反转",
        severity="major",
        impact="破坏必查目标",
        check=check,
    )
    submission = report.model_copy(
        update={
            "issues": (issue,),
            "dimensions": tuple(
                item.model_copy(update={"status": "major_gap", "issue_ids": ("I1",)}) if item.id == dimension else item for item in report.dimensions
            ),
        }
    )
    return ReviewModel(responses=[review_call("submit_review", {"report": submission.model_dump(mode="json")})])


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rendered = self.directory / "candidate.png"
        self.options = ReviewOptions(output_dir=self.directory / "runs", max_request_retries=0)

    def invoke(self, reference: Path, request: ReviewRequest, model: ReviewModel) -> tuple[ReviewOutcome, ReviewPackage]:
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            outcome = run_review(reference, self.rendered, request, options=self.options)
        self.assertEqual(outcome.status, "completed", outcome.error)
        return outcome, read_review_package(outcome.report_dir)

    def test_frosted_circle_measurements_in_model_input_and_formal_report(self) -> None:
        front, background = rect(805, 260, 821, 276), rect(1005, 272, 1021, 288)
        with Image.open(FROSTED) as original, original.copy() as rendered:
            rendered.paste((230, 230, 230), (front.left, front.top, front.right, front.bottom))
            rendered.save(self.rendered)
        request = task(
            "feature",
            {"color": ("比对前圆与背景的相对明暗",)},
            samples=(
                ReviewSample(label="前圆", reference_region=front, rendered_region=front),
                ReviewSample(label="背景", reference_region=background, rendered_region=background),
            ),
        )
        model = issue_model(request, "color")
        outcome, package = self.invoke(FROSTED, request, model)
        evidence = package.report.evidence
        first, second = evidence.samples
        self.assertEqual(first.reference.region, front)
        self.assertEqual(first.rendered.region, front)
        self.assertEqual(first.reference.pixel_count, 256)
        self.assertEqual(first.reference.mean_rgb, (219, 219, 219))
        self.assertEqual(first.reference.mean_luma - second.reference.mean_luma, -10)
        self.assertEqual(first.rendered.mean_luma - second.rendered.mean_luma, 1)
        self.assertEqual(evidence.units, "encoded_RGB_0_255")
        self.assertEqual(review_context(model.requests[0])["evidence"], evidence.model_dump(mode="json"))
        self.assertEqual(outcome.verdict, "needs_revision")
        self.assertEqual(package.report.verdict, outcome.verdict)
        self.assert_resealed_measurement_is_rejected(package.directory)

    def assert_resealed_measurement_is_rejected(self, directory: Path) -> None:
        report_path, manifest_path = directory / "review.json", directory / "manifest.json"
        report = json.loads(report_path.read_bytes())
        report["evidence"]["samples"][0]["reference"]["mean_luma"] += 1
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest = json.loads(manifest_path.read_bytes())
        contents = {name: (directory / name).read_bytes() for name in manifest["files"]}
        manifest["files"]["review.json"] = hashlib.sha256(contents["review.json"]).hexdigest()
        manifest["content_sha256"] = content_sha256(contents)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "报告证据"):
            read_review_package(directory)

    def gear_request(self, candidate: PixelRegion, *, with_hole: bool = True) -> ReviewRequest:
        outer = rect(200, 346, 322, 469)
        checks = (RIM, HOLE) if with_hole else (RIM,)
        return task(
            "element",
            {"shape": checks},
            reference_region=outer,
            coverage=ReviewCoverage(
                reference_window=rect(0, 0, 1280, 720),
                rendered_window=candidate,
                required_regions=(RequiredRegion(dimension="shape", check=RIM, region=outer),),
            ),
        )

    def test_gear_window_rejects_invented_outer_rim_issue_and_records_unassessed(self) -> None:
        window = rect(224, 374, 300, 440)
        with Image.open(GEAR) as original, original.crop((224, 374, 300, 440)) as rendered:
            rendered.save(self.rendered)
        request = self.gear_request(window)
        model = issue_model(request, "shape", RIM)
        outcome, package = self.invoke(GEAR, request, model)
        self.assertEqual(outcome.execution["validation_errors"], 1)
        self.assertEqual(outcome.verdict, "inconclusive")
        self.assertEqual(package.report.verdict, outcome.verdict)
        self.assertFalse(package.report.submission.issues)
        coverage = package.report.evidence.coverage
        self.assertEqual(coverage.rendered_view, window)
        self.assertEqual(coverage.reference_view, request.reference_region)
        self.assertEqual(coverage.unassessed, package.report.submission.unassessed)
        self.assertEqual(coverage.unassessed[0].subject, RIM)
        self.assertIn("完整视图", coverage.unassessed[0].reason)
        self.assertEqual(review_context(model.requests[0])["evidence"]["coverage"], coverage.model_dump(mode="json"))

        # 同一维度有已取证的中心孔时, 不因外缘未评而丢掉真正可见的差异.
        with Image.open(self.rendered) as crop, crop.copy() as painted:
            ImageDraw.Draw(painted).ellipse((14, 9, 62, 57), fill=(68, 188, 99))
            painted.save(self.rendered)
        outcome, package = self.invoke(GEAR, request, issue_model(request, "shape", HOLE))
        self.assertEqual(outcome.verdict, "needs_revision")
        self.assertEqual(package.report.submission.issues[0].check, HOLE)
        self.assertEqual(package.report.submission.unassessed[0].subject, RIM)

    def test_full_gear_view_keeps_issue_for_changed_outer_rim(self) -> None:
        with Image.open(GEAR) as original, original.copy() as rendered:
            ImageDraw.Draw(rendered).ellipse((211, 356, 313, 458), fill=(68, 188, 99))
            with original.crop((242, 387, 283, 428)) as center:
                rendered.paste(center, (242, 387))
            rendered.save(self.rendered)
        bounds = rect(0, 0, 1280, 720)
        request = self.gear_request(bounds).model_copy(update={"rendered_region": rect(200, 346, 322, 469)})
        outcome, package = self.invoke(GEAR, request, issue_model(request, "shape", RIM))
        self.assertEqual(outcome.verdict, "needs_revision")
        self.assertFalse(package.report.evidence.coverage.unassessed)
        self.assertEqual(package.report.submission.issues[0].check, RIM)

    def test_all_uncovered_has_formal_report_without_provider_and_bad_mapping_fails_early(self) -> None:
        window = rect(224, 374, 300, 440)
        with Image.open(GEAR) as original, original.crop((224, 374, 300, 440)) as rendered:
            rendered.save(self.rendered)
        with patch("shader_deep.workflows.review.entry.build_model") as build:
            outcome = run_review(GEAR, self.rendered, self.gear_request(window, with_hole=False), options=self.options)
            self.assertEqual(outcome.verdict, "inconclusive")
            self.assertEqual(read_review_package(outcome.report_dir).report.submission.unassessed[0].subject, RIM)
            with self.assertRaisesRegex(ValueError, "窗口尺寸"):
                run_review(GEAR, self.rendered, self.gear_request(rect(0, 0, 1280, 720)), options=self.options)
        build.assert_not_called()
