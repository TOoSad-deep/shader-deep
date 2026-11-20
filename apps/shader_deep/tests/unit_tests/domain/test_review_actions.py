"""评审行动摘要以被引用的原报告条目作为唯一来源."""

from __future__ import annotations

from unittest import TestCase

from pydantic import ValidationError

from shader_deep.domain.review_actions import ReviewActionSummary, summarize_review_actions
from shader_deep.domain.visual_review import ReviewIssue, UnassessedItem, derive_verdict, validate_review_submission
from tests.unit_tests.fixtures._review_fixture import matched_review, review_criteria, review_request


class ReviewActionTests(TestCase):
    def test_cross_dimension_minor_issues_count_once_without_escalation(self) -> None:
        criteria = review_criteria(review_request(required=("shape", "relations")))
        baseline = matched_review(criteria)
        issues = tuple(
            ReviewIssue(
                id=f"I{index}",
                dimension="shape",
                region=region,
                reference_observation="曲线较平",
                rendered_observation="略凸",
                severity="minor",
                impact="轮廓有轻微偏差",
            )
            for index, region in enumerate(("左", "中", "右"))
        )
        dims = tuple(
            dim.model_copy(update={"status": "minor_gap", "issue_ids": tuple(issue.id for issue in issues) if dim.id == "shape" else ("I0",)})
            if dim.id in {"shape", "relations"}
            else dim
            for dim in baseline.dimensions
        )
        report = baseline.model_copy(update={"dimensions": dims, "issues": issues})
        validate_review_submission(report, criteria)
        actions = summarize_review_actions(report)
        self.assertEqual(actions.model_dump(mode="json"), {"minor_issue_count": 3, "has_minor_issues": True, "major_issues": [], "unassessed": []})
        self.assertEqual(derive_verdict(report), "no_major_issue_observed")

    def test_major_problem_and_unassessed_region_remain_actionable_together(self) -> None:
        criteria = review_criteria()
        baseline = matched_review(criteria)
        hole = ReviewIssue(
            id="缺孔",
            dimension="shape",
            region="中心",
            reference_observation="中心有孔",
            rendered_observation="中心填平",
            severity="major",
            impact="必要开口消失",
        )
        rim = UnassessedItem(id="外缘未评", dimension="shape", subject="外缘轮廓", reason="候选采集窗口未包含外缘")
        dims = tuple(
            dim.model_copy(update={"status": "major_gap", "issue_ids": (hole.id,), "unassessed_ids": (rim.id,)}) if dim.id == "shape" else dim
            for dim in baseline.dimensions
        )
        report = baseline.model_copy(update={"dimensions": dims, "issues": (hole,), "unassessed": (rim,)})
        validate_review_submission(report, criteria)
        actions = summarize_review_actions(report)
        self.assertEqual((actions.minor_issue_count, actions.has_minor_issues), (0, False))
        self.assertEqual((actions.major_issues, actions.unassessed), ((hole,), (rim,)))
        self.assertEqual(derive_verdict(report), "needs_revision")
        self.assertEqual(ReviewActionSummary.model_validate_json(actions.model_dump_json()), actions)

    def test_empty_report_has_no_actions_and_rejects_a_contradictory_minor_flag(self) -> None:
        actions = summarize_review_actions(matched_review()).model_dump(mode="json")
        self.assertEqual(actions, {"minor_issue_count": 0, "has_minor_issues": False, "major_issues": [], "unassessed": []})
        with self.assertRaises(ValidationError):
            ReviewActionSummary.model_validate({**actions, "has_minor_issues": True})
