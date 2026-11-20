"""逐项评审的不可变标识、范围约束和六维汇总."""

from __future__ import annotations

from unittest import TestCase

from pydantic import ValidationError

from shader_deep.domain.review_items import CheckAnswer, CheckFinding, bound_checks, summarize_checks, validate_check_answer
from shader_deep.domain.visual_review import (
    BoundReviewCriteria,
    CoverageEvidence,
    DimensionCriteria,
    PixelRegion,
    ReviewRequest,
    ReviewValidationError,
    UnassessedItem,
    derive_verdict,
    validate_bound_criteria,
)
from tests.unit_tests.fixtures._review_fixture import review_criteria, review_request


def _criteria(*checks: str) -> BoundReviewCriteria:
    request = review_request()
    dimensions = tuple(DimensionCriteria(id="shape", required_checks=checks) if item.id == "shape" else item for item in request.dimensions)
    return review_criteria(request.model_copy(update={"protocol": "visual_review_v2", "dimensions": dimensions}))


def _coverage(check: str) -> CoverageEvidence:
    region = PixelRegion(left=0, top=0, right=2, bottom=2)
    gap = UnassessedItem(id="program-coverage-1", dimension="shape", subject=check, reason="外缘不在采集窗内")
    return CoverageEvidence(reference_view=region, rendered_view=region, unassessed=(gap,))


class ReviewItemsTests(TestCase):
    def test_historical_v1_identity_and_duplicate_compatibility(self) -> None:
        historical = review_criteria()
        self.assertEqual(historical.criteria_sha256, "099a9de630aa74215933c3d57dab50bca25e66afbeca3a8e56e8ecbe09481031")
        self.assertNotIn("protocol", historical.request.model_dump(mode="json"))
        self.assertNotIn("checks", historical.model_dump(mode="json"))
        duplicate = DimensionCriteria(id="shape", required_checks=("中心孔", "中心孔"))
        dims = tuple(duplicate if item.id == "shape" else item for item in historical.request.dimensions)
        old = ReviewRequest.model_validate({**historical.request.model_dump(), "dimensions": dims, "protocol": "visual_review_v1"})
        self.assertEqual(old.dimensions[2].required_checks, duplicate.required_checks)
        with self.assertRaises(ValidationError):
            _criteria("中心孔", "中心孔")

    def test_check_cannot_be_both_required_and_excluded_in_v2(self) -> None:
        request = review_request()
        conflict = DimensionCriteria(id="shape", required_checks=("中心孔",), excluded_checks=("中心孔",))
        dims = tuple(conflict if item.id == "shape" else item for item in request.dimensions)
        with self.assertRaisesRegex(ValidationError, "同时列为必查和排除: 中心孔"):
            ReviewRequest.model_validate({**request.model_dump(), "dimensions": dims, "protocol": "visual_review_v2"})

    def test_unrelated_check_order_and_coverage_do_not_change_check_ids(self) -> None:
        first = bound_checks(_criteria("中心孔", "外缘"), None)
        second = bound_checks(_criteria("外缘", "中心孔"), _coverage("外缘"))
        self.assertEqual([(item.check, item.check_id) for item in first], [(item.check, item.check_id) for item in reversed(second)])
        self.assertIsNotNone(second[0].unassessed)
        self.assertIsNone(second[1].unassessed)
        self.assertNotEqual(first[0].check_id, first[1].check_id)

    def test_bound_json_contains_verified_mapping_for_answers_and_locked_checks(self) -> None:
        criteria = _criteria("中心孔", "外缘")
        payload = criteria.model_dump(mode="json")
        checks = bound_checks(criteria, _coverage("外缘"))
        self.assertEqual(payload["checks"], [{"check_id": check.check_id, "dimension": "shape", "check": check.check} for check in checks])
        restored = BoundReviewCriteria.model_validate(payload)
        validate_bound_criteria(restored, "共同规则", "整图模板")
        payload["checks"][0]["check_id"] = checks[1].check_id
        with self.assertRaises(ReviewValidationError):
            validate_bound_criteria(BoundReviewCriteria.model_validate(payload), "共同规则", "整图模板")

    def test_incomplete_duplicate_unknown_and_locked_answers_rejected_with_ids(self) -> None:
        criteria, coverage = _criteria("中心孔", "齿形", "外缘"), _coverage("外缘")
        checks = bound_checks(criteria, coverage)
        first, second, locked = (CheckAnswer(check_id=item.check_id, rationale="按固定视图检查") for item in checks)
        with self.assertRaises(ReviewValidationError) as missing:
            summarize_checks(criteria, coverage, (), summary="结果")
        self.assertIn(first.check_id, missing.exception.message)
        self.assertIn(second.check_id, missing.exception.message)
        self.assertNotIn(locked.check_id, missing.exception.message)
        with self.assertRaises(ReviewValidationError) as duplicate:
            summarize_checks(criteria, coverage, (first, second, first), summary="结果")
        self.assertEqual(duplicate.exception.code, "duplicate_answer")
        for submitted, expected in ((CheckAnswer(check_id="背景网格", rationale="范围外"), "unknown_check"), (locked, "locked_check")):
            with self.subTest(expected=expected), self.assertRaises(ReviewValidationError) as error:
                validate_check_answer(submitted, checks)
            self.assertEqual(error.exception.code, expected)
            self.assertIn(submitted.check_id, error.exception.path)

    def test_findings_and_both_sources_of_unknowns_preserve_their_evidence(self) -> None:
        criteria, coverage = _criteria("中心孔", "曲率", "外缘"), _coverage("外缘")
        hole, curve, _ = bound_checks(criteria, coverage)
        major = CheckFinding(
            region="孔心", reference_observation="背景可见", rendered_observation="孔被填实", severity="major", impact="必要孔口消失"
        )
        minor = CheckFinding(region="左沿", reference_observation="光滑", rendered_observation="略有凸起", severity="minor", impact="边缘不匀")
        first = CheckAnswer(check_id=hole.check_id, rationale="见孔心和左沿", findings=(major, minor))
        second = CheckAnswer(check_id=curve.check_id, rationale="浅色边界难以分辨", unassessed_reason="没有足够对比")
        report = summarize_checks(criteria, coverage, (second, first), summary="中心孔须修改", limitations=("单帧",))
        self.assertEqual(report, summarize_checks(criteria, coverage, (first, second), summary=report.summary, limitations=report.limitations))
        self.assertEqual(derive_verdict(report), "needs_revision")
        self.assertEqual([(issue.check, issue.severity) for issue in report.issues], [("中心孔", "major"), ("中心孔", "minor")])
        self.assertEqual([(gap.subject, gap.reason) for gap in report.unassessed], [("曲率", "没有足够对比"), ("外缘", "外缘不在采集窗内")])
        self.assertIn(coverage.unassessed[0], report.unassessed)
        shape = next(dim for dim in report.dimensions if dim.id == "shape")
        self.assertEqual(shape.issue_ids, tuple(issue.id for issue in report.issues))
        self.assertEqual(shape.unassessed_ids, tuple(gap.id for gap in report.unassessed))
        for rationale in (first.rationale, second.rationale, coverage.unassessed[0].reason):
            self.assertIn(rationale, shape.rationale)
        self.assertTrue(
            all(dim.status == "not_applicable" and dim.rationale == "本次只检查指定表现" for dim in report.dimensions if dim.id != "shape")
        )

    def test_all_locked_has_no_model_answers_and_unassessed_is_a_complete_report(self) -> None:
        criteria, coverage = _criteria("外缘"), _coverage("外缘")
        report = summarize_checks(criteria, coverage, (), summary="需补外缘视图")
        self.assertEqual(derive_verdict(report), "inconclusive")
        self.assertEqual(report.unassessed, coverage.unassessed)
        self.assertEqual(report.summary, "需补外缘视图")
