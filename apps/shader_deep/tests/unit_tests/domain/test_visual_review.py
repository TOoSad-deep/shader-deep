"""视觉评审固定范围、证据闭合与派生结论的行为."""

from __future__ import annotations

import math
from unittest import TestCase

from pydantic import ValidationError

from shader_deep.domain.visual_review import (
    DIMENSION_IDS,
    BoundReviewCriteria,
    DimensionCriteria,
    DimensionReview,
    PixelRegion,
    ReviewConditions,
    ReviewIssue,
    ReviewRequest,
    ReviewSource,
    ReviewSubmission,
    ReviewValidationError,
    UnassessedItem,
    bind_review_criteria,
    derive_verdict,
    review_profile,
    validate_bound_criteria,
    validate_review_submission,
)
from tests.unit_tests.fixtures._review_fixture import matched_review, review_criteria, review_request


def _issue(severity: str = "major") -> dict[str, str]:
    return {
        "id": "I1",
        "dimension": "shape",
        "region": "圆环开口",
        "reference_observation": "开口可见",
        "rendered_observation": "开口消失",
        "severity": severity,
        "impact": "必要开口未重建",
    }


class VisualReviewTests(TestCase):
    def test_only_valid_scope_type_combinations_route(self) -> None:
        data = review_request().model_dump(mode="json")
        for scope, kind, expected in (("scene", None, "scene"), ("local", "feature", "feature"), ("local", "element", "element")):
            with self.subTest(scope=scope, kind=kind):
                request = ReviewRequest.model_validate({**data, "scope": scope, "target_kind": kind})
                self.assertEqual(review_profile(request), expected)
        for scope, kind in (("local", None), ("scene", "feature"), ("other", None), ("local", "scene")):
            with self.subTest(scope=scope, kind=kind), self.assertRaises(ValidationError):
                ReviewRequest.model_validate({**data, "scope": scope, "target_kind": kind})

    def test_binding_copies_mutable_input_and_normalizes_dimension_order(self) -> None:
        data = review_request().model_dump(mode="json")
        data["dimensions"].reverse()
        criteria = bind_review_criteria(ReviewRequest.model_validate(data), "共同规则", "整图模板")
        data["dimensions"][0]["excluded_checks"].append("事后改变范围")
        self.assertEqual(tuple(item.id for item in criteria.request.dimensions), DIMENSION_IDS)
        self.assertEqual(criteria, review_criteria())
        self.assertIsInstance(criteria.request.dimensions, tuple)
        self.assertIsInstance(criteria.request.dimensions[0].excluded_checks, tuple)
        with self.assertRaises(ValidationError):
            criteria.request.target = "新目标"

    def test_binding_identity_changes_with_requirements_or_rules(self) -> None:
        request = review_request()
        criteria = review_criteria(request)
        variants = (
            bind_review_criteria(request.model_copy(update={"requirements": "新要求"}), "共同规则", "整图模板"),
            bind_review_criteria(request, "规则修订", "整图模板"),
            bind_review_criteria(request, "共同规则", "模板修订"),
        )
        self.assertTrue(all(item.criteria_sha256 != criteria.criteria_sha256 for item in variants))
        validate_bound_criteria(criteria, "共同规则", "整图模板")
        with self.assertRaises(ReviewValidationError):
            validate_bound_criteria(criteria, "共同规则", "模板修订")
        changed = BoundReviewCriteria.model_validate({**criteria.model_dump(mode="json"), "profile_id": "feature"})
        with self.assertRaises(ReviewValidationError):
            validate_bound_criteria(changed, "共同规则", "整图模板")

    def test_source_candidate_is_retained_without_changing_criteria_identity(self) -> None:
        first = review_request()
        second = first.model_copy(update={"source": ReviewSource(candidate_id="candidate-2", run_id="run-2")})
        original, changed = review_criteria(first), review_criteria(second)
        self.assertEqual(original.criteria_sha256, changed.criteria_sha256)
        self.assertEqual(changed.request.source.candidate_id, "candidate-2")
        self.assertNotEqual(original.request, changed.request)
        validate_bound_criteria(changed, "共同规则", "整图模板")

    def test_task_rejects_blank_unknown_empty_or_duplicate_criteria(self) -> None:
        data = review_request().model_dump(mode="json")
        invalid = (
            {**data, "target": "  "},
            {**data, "unexpected": True},
            {**data, "dimensions": [data["dimensions"][0]] * 6},
            {**data, "dimensions": [{"id": item, "excluded_checks": ["范围外"]} for item in DIMENSION_IDS]},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReviewRequest.model_validate(value)
        with self.assertRaises(ValidationError):
            DimensionCriteria(id="shape")

    def test_region_and_time_reject_coercions_and_nonfinite_values(self) -> None:
        for left in (True, "1", -1):
            with self.subTest(left=left), self.assertRaises(ValidationError):
                PixelRegion.model_validate({"left": left, "top": 0, "right": 2, "bottom": 2})
        with self.assertRaises(ValidationError):
            PixelRegion(left=1, top=0, right=1, bottom=2)
        for value in (True, "1", math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReviewConditions.model_validate({"rendered_time": value})

    def test_derived_verdict_preserves_known_problem_and_unknown_content(self) -> None:
        criteria = review_criteria()
        for severity, unknown, verdict in (
            ("major", True, "needs_revision"),
            ("minor", True, "inconclusive"),
            ("minor", False, "no_major_issue_observed"),
            (None, True, "inconclusive"),
            (None, False, "no_major_issue_observed"),
        ):
            with self.subTest(severity=severity, unknown=unknown):
                issue = () if severity is None else (ReviewIssue.model_validate(_issue(severity)),)
                missing = (UnassessedItem(id="U1", dimension="shape", subject="背面轮廓", reason="被遮挡"),) if unknown else ()
                status = "major_gap" if severity == "major" else "minor_gap" if severity else "uncertain" if unknown else "match"
                baseline = matched_review(criteria)
                dimensions = tuple(
                    DimensionReview(
                        id="shape",
                        status=status,
                        rationale="依据可见轮廓",
                        issue_ids=("I1",) if issue else (),
                        unassessed_ids=("U1",) if missing else (),
                    )
                    if item.id == "shape"
                    else item
                    for item in baseline.dimensions
                )
                submission = ReviewSubmission(summary="本轮结果", dimensions=dimensions, issues=issue, unassessed=missing)
                validate_review_submission(submission, criteria)
                self.assertEqual(derive_verdict(submission), verdict)

    def test_unsupported_gap_statuses_and_required_na_are_rejected(self) -> None:
        criteria = review_criteria()
        baseline = matched_review(criteria)
        for status in ("minor_gap", "major_gap", "uncertain", "not_applicable"):
            with self.subTest(status=status), self.assertRaises(ReviewValidationError):
                changed = tuple(item.model_copy(update={"status": status}) if item.id == "shape" else item for item in baseline.dimensions)
                validate_review_submission(baseline.model_copy(update={"dimensions": changed}), criteria)

    def test_orphan_and_missing_references_are_rejected(self) -> None:
        criteria = review_criteria()
        baseline = matched_review(criteria)
        issue = ReviewIssue.model_validate(_issue())
        with self.assertRaises(ReviewValidationError):
            validate_review_submission(baseline.model_copy(update={"issues": (issue,)}), criteria)
        changed = tuple(item.model_copy(update={"issue_ids": ("I9",)}) if item.id == "shape" else item for item in baseline.dimensions)
        with self.assertRaises(ReviewValidationError):
            validate_review_submission(baseline.model_copy(update={"dimensions": changed}), criteria)

    def test_excluded_dimension_cannot_introduce_issue_or_unknown(self) -> None:
        criteria = review_criteria()
        baseline = matched_review(criteria)
        missing = UnassessedItem(id="U1", dimension="color", subject="色值", reason="看不清")
        changed = tuple(
            item.model_copy(update={"status": "uncertain", "unassessed_ids": ("U1",)}) if item.id == "color" else item for item in baseline.dimensions
        )
        with self.assertRaises(ReviewValidationError):
            validate_review_submission(baseline.model_copy(update={"dimensions": changed, "unassessed": (missing,)}), criteria)

    def test_duplicate_dimensions_ids_and_references_are_rejected(self) -> None:
        criteria = review_criteria()
        baseline = matched_review(criteria)
        issue = ReviewIssue.model_validate(_issue())
        for submission in (
            baseline.model_copy(update={"dimensions": (baseline.dimensions[0],) * 6}),
            baseline.model_copy(update={"issues": (issue, issue)}),
            baseline.model_copy(
                update={
                    "dimensions": tuple(
                        item.model_copy(update={"issue_ids": ("I1", "I1")}) if item.id == "shape" else item for item in baseline.dimensions
                    ),
                    "issues": (issue,),
                }
            ),
        ):
            with self.subTest(submission=submission), self.assertRaises(ReviewValidationError):
                validate_review_submission(submission, criteria)

    def test_cross_dimension_issue_preserves_single_problem(self) -> None:
        criteria = review_criteria(review_request(required=("shape", "layout")))
        issue = ReviewIssue.model_validate(_issue())
        baseline = matched_review(criteria)
        dimensions = tuple(
            item.model_copy(update={"status": "major_gap", "issue_ids": ("I1",)}) if item.id in {"shape", "layout"} else item
            for item in baseline.dimensions
        )
        submission = baseline.model_copy(update={"dimensions": dimensions, "issues": (issue,)})
        validate_review_submission(submission, criteria)
        self.assertEqual(len(submission.issues), 1)
        self.assertEqual(derive_verdict(submission), "needs_revision")
