"""验证 V2 显式批量收尾的正式交付、审计和失败原子性."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from PIL import Image

from shader_deep.api import PixelRegion, RequiredRegion, ReviewCoverage, ReviewOptions, read_review_package, republish_review, run_review
from shader_deep.domain.review_items import CheckAnswer
from shader_deep.domain.visual_review import ReviewValidationError
from shader_deep.infrastructure.storage.review_package import capture_review_source
from shader_deep.workflows.review.session import ReviewSession
from tests.unit_tests.fixtures._review_fixture import review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call


class ReviewBatchFinishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.png = self.root / "view.png"
        with Image.new("RGB", (4, 4), "white") as image:
            image.save(self.png)
        window = PixelRegion(left=0, top=0, right=4, bottom=4)
        missing = RequiredRegion(dimension="layout", check="符合参考表现", region=PixelRegion(left=4, top=0, right=8, bottom=4))
        coverage = ReviewCoverage(reference_window=window, rendered_window=window, required_regions=(missing,))
        self.task = review_request(required=("layout", "shape", "color", "surface")).model_copy(
            update={"protocol": "visual_review_v2", "coverage": coverage}
        )
        source = capture_review_source(self.png, self.png, self.root / "fixed", self.task)
        self.options = ReviewOptions(output_dir=self.root / "runs", max_model_calls=1)
        self.session = ReviewSession(source, self.options)
        self.answers = {item.dimension: CheckAnswer(check_id=item.check_id, rationale="可见区域一致") for item in self.session.checks}

    def test_one_finish_call_delivers_all_answers_in_frozen_order_and_can_republish(self) -> None:
        shape, color, surface = (self.answers[key] for key in ("shape", "color", "surface"))
        args = {"summary": "可见的三项均一致", "answers": [item.model_dump(mode="json") for item in (surface, color, shape)]}
        model = ReviewModel(responses=[review_call("finish_review", args)])
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            outcome = run_review(self.png, self.png, self.task, options=self.options)
        self.assertEqual((outcome.status, outcome.verdict, len(model.requests)), ("completed", "inconclusive", 1), outcome.error)
        package = read_review_package(outcome.report_dir)
        self.assertEqual(package.report.check_answers, (shape, color, surface))
        self.assertEqual([item.dimension for item in package.report.submission.unassessed], ["layout"])
        accepted = json.loads((outcome.run_dir / "results/accepted.json").read_bytes())
        self.assertEqual(accepted["check_answers"], [item.model_dump(mode="json") for item in (shape, color, surface)])
        with patch("shader_deep.workflows.review.entry.build_model", side_effect=AssertionError("重发不得调模型")):
            republished = republish_review(outcome.run_dir)
        self.assertEqual(read_review_package(republished.report_dir).content_sha256, package.content_sha256)

    def test_finish_can_add_only_remaining_answers_after_an_explicit_revision(self) -> None:
        shape, color, surface = (self.answers[key] for key in ("shape", "color", "surface"))
        changed = shape.model_copy(update={"rationale": "复查后确认上缘可见"})
        calls = [
            review_call("record_review_check", {"answer": shape.model_dump(mode="json")}, "first"),
            review_call(
                "revise_review_check",
                {"answer": changed.model_dump(mode="json"), "expected_revision": 1, "reason": "复核上缘", "evidence": "双图顶行相同"},
                "revise",
            ),
            review_call(
                "finish_review",
                {"summary": "按复核结果交付", "limitations": [], "answers": [item.model_dump(mode="json") for item in (surface, color)]},
                "finish",
            ),
        ]
        response = AIMessage(content="", tool_calls=[message.tool_calls[0] for message in calls])
        with patch("shader_deep.workflows.review.entry.build_model", return_value=ReviewModel(responses=[response])):
            outcome = run_review(self.png, self.png, self.task, options=self.options)
        self.assertEqual(outcome.status, "completed", outcome.error)
        report = read_review_package(outcome.report_dir).report
        self.assertEqual(report.check_answers, (changed, color, surface))
        audit = report.metadata["check_revisions"]
        self.assertEqual([(item["before"], item["after"]) for item in audit], [(shape.model_dump(mode="json"), changed.model_dump(mode="json"))])
        accepted = json.loads((outcome.run_dir / "results/accepted.json").read_bytes())
        self.assertEqual(accepted["check_revisions"], audit)
        self.assertEqual(accepted["publication_seed"]["execution"]["check_revisions"], audit)

    def test_invalid_batch_and_failed_write_leave_prior_answers_intact_until_success(self) -> None:
        session = self.session
        shape, color, surface = (self.answers[key] for key in ("shape", "color", "surface"))
        session.record_check(shape)
        progress, accepted = (session.directory / "results" / name for name in ("check_answers.json", "accepted.json"))
        initial = progress.read_bytes()
        cases = [
            ("missing_answers", (color,)),
            ("duplicate_answer", (color, shape, surface)),
            ("duplicate_answer", (color, shape.model_copy(update={"rationale": "改写已答项"}), surface)),
            ("duplicate_answer", (color, surface, color)),
            ("unknown_check", (color, surface, shape.model_copy(update={"check_id": "unknown"}))),
            ("locked_check", (color, surface, self.answers["layout"])),
        ]
        for code, answers in cases:
            with self.subTest(code=code, answers=answers):
                with self.assertRaises(ReviewValidationError) as failure:
                    session.finish("核对完成", answers=answers)
                self.assertEqual(failure.exception.code, code)
                self.assertEqual((session.state, session.check_answers, session.submission), ("running", (shape,), None))
                self.assertEqual(progress.read_bytes(), initial)
                self.assertFalse(accepted.exists())
        with patch("shader_deep.workflows.review.session.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            session.finish("核对完成", answers=(surface, color))
        self.assertEqual((session.state, session.check_answers, session.submission), ("running", (shape,), None))
        self.assertEqual(progress.read_bytes(), initial)
        self.assertFalse(accepted.exists())
        session.finish("核对完成", answers=(surface, color))
        self.assertEqual((session.state, session.check_answers, accepted.is_file()), ("accepted", (shape, color, surface), True))
