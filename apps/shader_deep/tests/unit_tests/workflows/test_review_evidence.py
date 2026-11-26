"""从公开入口核对测量、程序未评项和正式报告的完整链路."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import patch

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

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
from shader_deep.domain.evidence import ImageRegion
from shader_deep.domain.scene_observations import ReferenceMeasurement
from shader_deep.domain.visual_review import DIMENSION_IDS, ReviewIssue
from shader_deep.infrastructure.storage.planning_inputs import content_sha256
from shader_deep.workflows.review import freeze_workflow_review, run_workflow_review
from tests.unit_tests.fixtures._review_fixture import matched_review, review_criteria, review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import BoundReviewCriteria, DimensionId
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
        context = review_context(model.requests[0])
        self.assertEqual(context["evidence"], evidence.model_dump(mode="json"))
        self.assertNotIn("coverage.unassessed", context["evidence_notes"])
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
        context = review_context(model.requests[0])
        self.assertEqual(context["evidence"]["coverage"], coverage.model_dump(mode="json"))
        self.assertNotIn("units", context["evidence"])
        self.assertNotIn("alpha_background", context["evidence"])
        self.assertNotIn("均值", context["evidence_notes"])

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

    def test_issue_bound_to_the_wrong_check_is_rejected_without_coverage(self) -> None:
        request = task("feature", {"shape": (RIM,), "color": ("天空颜色",)})
        with Image.new("RGB", (8, 8)) as image:
            image.save(self.rendered)
        model = issue_model(request, "shape", "天空颜色")
        model.responses.extend(issue_model(request, "shape", RIM).responses)
        outcome, package = self.invoke(self.rendered, request, model)
        self.assertEqual(outcome.execution["validation_errors"], 1)
        self.assertEqual([issue.check for issue in package.report.submission.issues], [RIM])

    def test_all_uncovered_has_formal_report_without_provider_and_bad_mapping_fails_early(self) -> None:
        window = rect(224, 374, 300, 440)
        with Image.open(GEAR) as original, original.crop((224, 374, 300, 440)) as rendered:
            rendered.save(self.rendered)
        with patch("shader_deep.workflows.review.entry.build_model") as build:
            coverage = self.gear_request(window, with_hole=False).coverage
            for checks in ((RIM,), (RIM, RIM)):
                with self.subTest(checks=checks):
                    request = task("element", {"shape": checks}, coverage=coverage)
                    outcome = run_review(GEAR, self.rendered, request, options=self.options)
                    self.assertEqual(outcome.verdict, "inconclusive", outcome.error)
                    report = read_review_package(outcome.report_dir).report
                    self.assertEqual([gap.subject for gap in report.submission.unassessed], [RIM])
            with self.assertRaisesRegex(ValueError, "窗口尺寸"):
                run_review(GEAR, self.rendered, self.gear_request(rect(0, 0, 1280, 720)), options=self.options)
        build.assert_not_called()


class WorkflowReviewEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.reference, self.candidate = directory / "reference.png", directory / "candidate.png"
        self.options = ReviewOptions(output_dir=directory / "runs", max_request_retries=0)
        for path, color in ((self.reference, "red"), (self.candidate, "blue")):
            with Image.new("RGB", (96, 64), color) as image:
                image.save(path)
        request = review_request()
        self.request = request.model_copy(
            update={
                "dimensions": tuple(item.model_copy(update={"source_refs": ("E1",)}) if item.required_checks else item for item in request.dimensions)
            }
        )

    def anchor(self, path: Path, rgb: tuple[int, int, int] | None = None) -> ReferenceMeasurement:
        return ReferenceMeasurement(
            id="obs-1",
            kind="region_stats" if rgb else "crop",
            region=ImageRegion(left=24, top=18, right=30, bottom=22),
            question="测量目标位置",
            image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            mean_rgb=rgb,
            mean_luma=(rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722) if rgb else None,
        )

    def finish_model(self, criteria: BoundReviewCriteria) -> ReviewModel:
        return ReviewModel(
            responses=[
                review_call(
                    "finish_review",
                    {
                        "summary": "本轮已核对",
                        "answers": [{"check_id": item.check_id, "rationale": "已核对指定部位", "findings": []} for item in criteria.checks],
                        "limitations": [],
                    },
                )
            ]
        )

    def model_view(self, model: ReviewModel, index: int) -> Image.Image:
        blocks = [
            block
            for message in model.requests[0]
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "image_url"
        ]
        url = blocks[index]["image_url"]["url"]
        return Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))

    def test_same_and_swapped_images_keep_role_bars_and_raw_pairing(self) -> None:
        criteria = freeze_workflow_review(self.request)
        for reference, candidate, first_color, second_color in (
            (self.reference, self.reference, (255, 0, 0), (255, 0, 0)),
            (self.reference, self.candidate, (255, 0, 0), (0, 0, 255)),
            (self.candidate, self.reference, (0, 0, 255), (255, 0, 0)),
        ):
            with self.subTest(reference=reference.name, candidate=candidate.name):
                model = self.finish_model(criteria)
                with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
                    outcome = run_workflow_review(
                        reference,
                        candidate,
                        criteria,
                        options=self.options,
                        reference_measurements=(self.anchor(reference, first_color),),
                        optimization_regions=(rect(35, 27, 43, 34),),
                    )
                self.assertEqual(outcome.status, "completed", outcome.error)
                package = read_review_package(outcome.report_dir)
                self.assertEqual(package.source.workflow_criteria, criteria)
                self.assertEqual(package.report.criteria.checks, criteria.checks)
                self.assertEqual((package.directory / "reference.png").read_bytes(), reference.read_bytes())
                self.assertEqual((package.directory / "rendered.png").read_bytes(), candidate.read_bytes())
                with self.model_view(model, 0) as left, self.model_view(model, 1) as right:
                    self.assertNotEqual(left.getpixel((0, 0)), right.getpixel((0, 0)))
                    self.assertEqual(left.getpixel((0, left.height - 64)), (*first_color, 255))
                    self.assertEqual(right.getpixel((0, right.height - 64)), (*second_color, 255))
                evidence = package.report.evidence
                self.assertEqual(len(evidence.samples), 2)
                self.assertEqual(evidence.samples[0].reference.mean_rgb, first_color)
                self.assertEqual(evidence.samples[0].rendered.mean_rgb, second_color)
                context = review_context(model.requests[0])
                self.assertEqual(context["answerable_checks"][0]["source_ref"], "E1")
                self.assertEqual(context["evidence"], evidence.model_dump(mode="json"))

    def test_model_role_bars_keep_color_metadata_in_full_and_cropped_views(self) -> None:
        info = PngInfo()
        info.add(b"gAMA", struct.pack(">I", 100000))
        info.add(b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000))
        with Image.new("RGBA", (96, 64), (128, 100, 90, 170)) as reference, Image.new("LA", (96, 64), (85, 190)) as candidate:
            reference.save(self.reference, pnginfo=info)
            candidate.save(self.candidate, icc_profile=b"fixed grayscale ICC profile bytes")
        crop = rect(24, 18, 32, 24)
        request = self.request.model_copy(update={"scope": "local", "target_kind": "feature", "reference_region": crop, "rendered_region": crop})
        criteria = freeze_workflow_review(request)
        model = self.finish_model(criteria)
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            outcome = run_workflow_review(self.reference, self.candidate, criteria, options=self.options)
        self.assertEqual(outcome.status, "completed", outcome.error)
        if outcome.report_dir is None:
            self.fail("评审完成后应发布正式报告")
        package = read_review_package(outcome.report_dir)
        for role, path, mode, indexes in (
            ("reference", self.reference, "RGBA", (0, 2)),
            ("rendered", self.candidate, "LA", (1, 3)),
        ):
            self.assertEqual((package.directory / f"{role}.png").read_bytes(), path.read_bytes())
            with Image.open(path) as raw:
                for index, height in zip(indexes, (64, 6), strict=True):
                    with self.model_view(model, index) as labelled:
                        self.assertEqual(labelled.mode, mode)
                        self.assertEqual(labelled.info, raw.info)
                        self.assertEqual(labelled.getpixel((0, labelled.height - height)), raw.getpixel((0, 0)))

    def test_same_png_expansion_preserves_frozen_checks_and_recovers_sampling(self) -> None:
        window = rect(0, 0, 96, 64)
        crop = rect(0, 0, 12, 12)
        required = RequiredRegion(dimension="shape", check="符合参考表现", region=rect(24, 18, 30, 22))
        criteria = freeze_workflow_review(
            self.request.model_copy(
                update={
                    "scope": "local",
                    "target_kind": "feature",
                    "reference_region": crop,
                    "rendered_region": crop,
                    "coverage": ReviewCoverage(reference_window=window, rendered_window=window, required_regions=(required,)),
                }
            )
        )
        args = (self.reference, self.candidate, criteria)
        model = self.finish_model(criteria)
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model) as build:
            anchors = (self.anchor(self.reference, (255, 0, 0)),)
            missing = run_workflow_review(*args, options=self.options, reference_measurements=anchors)
            build.assert_not_called()
            recovered = run_workflow_review(*args, options=self.options, reference_measurements=anchors, expand_views=True)
            build.assert_called_once()
        self.assertEqual(missing.verdict, "inconclusive")
        missing_report = read_review_package(missing.report_dir).report
        self.assertEqual(missing_report.submission.unassessed[0].id, "program-coverage-1")
        self.assertFalse(missing_report.evidence.samples)
        self.assertEqual(recovered.verdict, "no_major_issue_observed")
        recovered_package = read_review_package(recovered.report_dir)
        self.assertEqual(recovered_package.source.workflow_criteria, criteria)
        self.assertEqual(recovered_package.report.criteria.checks, criteria.checks)
        self.assertEqual(recovered_package.report.evidence.samples[0].reference.region, required.region)

    def test_gray16_crop_only_keeps_visual_review_and_explicit_sampling_uses_midgray(self) -> None:
        info = PngInfo()
        info.add(b"gAMA", struct.pack(">I", 100000))
        with Image.new("I;16", (96, 64), 32768) as image:
            image.save(self.reference, pnginfo=info)
        criteria = freeze_workflow_review(self.request)
        anchor = self.anchor(self.reference)
        for regions in ((), (rect(24, 18, 30, 22),)):
            model = self.finish_model(criteria)
            with self.subTest(regions=regions), patch("shader_deep.workflows.review.entry.build_model", return_value=model):
                outcome = run_workflow_review(
                    self.reference, self.candidate, criteria, options=self.options, reference_measurements=(anchor,), optimization_regions=regions
                )
                self.assertEqual(outcome.status, "completed", outcome.error)
                with self.model_view(model, 0) as labelled:
                    self.assertEqual(labelled.info["gamma"], 1.0)
                    self.assertEqual(labelled.getpixel((0, labelled.height - 64)), (128, 255))
                report = read_review_package(outcome.report_dir).report
                if regions:
                    self.assertEqual(report.evidence.samples[0].reference.mean_rgb, (128.0, 128.0, 128.0))
                else:
                    self.assertIsNone(report.evidence)

    def test_invalid_gate_criteria_and_misbound_anchors_fail_before_model_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_ref"):
            freeze_workflow_review(review_request())
        criteria = freeze_workflow_review(self.request)
        tampered = criteria.model_copy(update={"request": criteria.request.model_copy(update={"requirements": "候选发布后修改"})})
        with patch("shader_deep.workflows.review.entry.build_model") as build:
            with self.assertRaisesRegex(ValueError, "绑定内容"):
                run_workflow_review(self.reference, self.candidate, tampered, options=self.options)
            with self.assertRaisesRegex(ValueError, "图像身份"):
                run_workflow_review(
                    self.reference, self.candidate, criteria, options=self.options, reference_measurements=(self.anchor(self.candidate),)
                )
            build.assert_not_called()
