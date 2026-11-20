"""用公开评审入口和真实 SDK 图验证改判审计、版本冲突与异常写入."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image

from shader_deep.api import ReviewOptions, read_review_package, run_review
from shader_deep.domain.review_items import CheckAnswer, validate_check_revisions
from shader_deep.infrastructure.storage.review_package import capture_review_source
from shader_deep.workflows.review.session import ReviewSession
from tests.unit_tests.fixtures._review_fixture import review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context


def _batch(*calls: AIMessage) -> AIMessage:
    return AIMessage(content="", tool_calls=[call for part in calls for call in part.tool_calls])


def _revise(answer: dict, version: int, call_id: str) -> AIMessage:
    return review_call(
        "revise_review_check",
        {"answer": answer, "expected_revision": version, "reason": "重新对照孔的位置", "evidence": "中心区域参考为白孔, 候选为黑色实心"},
        call_id,
    )


class ReviewRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ref, self.candidate = self.root / "ref.png", self.root / "candidate.png"
        with Image.new("RGB", (5, 5), "black") as image:
            image.save(self.candidate)
            image.putpixel((2, 2), (255, 255, 255))
            image.save(self.ref)
        self.task = review_request(required=("shape", "color")).model_copy(update={"protocol": "visual_review_v2"})
        source = capture_review_source(self.ref, self.candidate, self.root / "fixed", self.task)
        self.session = ReviewSession(source, ReviewOptions(output_dir=self.root / "runs"))
        self.shape, self.color = [item.check_id for item in self.session.checks]

    def test_revisions_keep_other_answers_and_can_be_verified_from_public_report(self) -> None:
        first = {"check_id": self.shape, "rationale": "原判断两图都有孔"}
        color = {"check_id": self.color, "rationale": "背景均黑"}
        finding = {"region": "中心像素", "reference_observation": "白孔", "rendered_observation": "黑色", "severity": "major", "impact": "必要孔消失"}
        second = {**first, "rationale": "中心孔缺失", "findings": [finding]}
        third = {**second, "rationale": "在 (2,2) 可定位缺失的孔"}
        model = ReviewModel(
            responses=[
                _batch(review_call("record_review_check", {"answer": first}, "a"), review_call("record_review_check", {"answer": color}, "b")),
                _batch(review_call("record_review_check", {"answer": second}, "overwrite"), _revise(second, 1, "correct")),
                _batch(_revise(third, 1, "stale"), _revise(third, 2, "locate"), review_call("finish_review", {"summary": "中心孔缺失"}, "done")),
            ]
        )
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            result = run_review(self.ref, self.candidate, self.task, options=self.session.options)
        self.assertEqual((result.status, result.verdict), ("completed", "needs_revision"), result.error)
        self.assertEqual(result.execution["validation_errors"], 2)
        self.assertEqual(review_context(model.requests[2])["answer_revisions"], {self.shape: 2, self.color: 1})
        refused = [item for item in model.requests[2] if isinstance(item, ToolMessage) and item.tool_call_id == "overwrite"]
        self.assertEqual(json.loads(refused[0].content)["code"], "check_already_answered")
        package = read_review_package(result.report_dir)
        audit = package.report.metadata["check_revisions"]
        self.assertEqual([(item["model_call"], item["revision"]) for item in audit], [(2, 2), (3, 3)])
        self.assertEqual(audit[0]["before"]["rationale"], first["rationale"])
        self.assertEqual(audit[0]["after"], audit[1]["before"])
        self.assertEqual(audit[1]["after"]["rationale"], third["rationale"])
        self.assertTrue(all(item["reason"] and item["evidence"] for item in audit))
        final = {item.check_id: item for item in package.report.check_answers}
        self.assertEqual(final[self.color], CheckAnswer.model_validate(color))
        self.assertEqual(final[self.shape], CheckAnswer.model_validate(third))
        for file in ("results/check_answers.json", "results/accepted.json"):
            self.assertEqual(json.loads((result.run_dir / file).read_bytes())["check_revisions"], audit)
        accepted = json.loads((result.run_dir / "results/accepted.json").read_bytes())
        seed = accepted["publication_seed"]
        self.assertEqual(seed["run_id"], result.run_dir.name)
        self.assertEqual(seed["execution"]["model_calls"], 3)
        self.assertEqual(seed["execution"]["state"], "accepted")
        self.assertIsNone(seed["execution"]["token_usage"])
        tampered = deepcopy(package.report.metadata)
        tampered["check_revisions"][1]["before"]["rationale"] = "中间混入的答案"
        with self.assertRaisesRegex(ValueError, "起点"):
            validate_check_revisions(package.source.criteria, package.source.coverage, package.report.check_answers, tampered)
        impossible_budget = {**package.report.metadata, "max_model_calls": 1}
        with self.assertRaisesRegex(ValueError, "预算"):
            validate_check_revisions(package.source.criteria, package.source.coverage, package.report.check_answers, impossible_budget)

    def test_revision_refuses_unrecorded_or_finished_checks_and_does_not_commit_failed_writes(self) -> None:
        session = self.session
        session.begin_model_call()
        answer = CheckAnswer(check_id=self.shape, rationale="已见圆孔")
        changed = answer.model_copy(update={"rationale": "孔缺失"})
        with self.assertRaisesRegex(ValueError, "尚未登记"):
            session.revise_check(changed, 1, "复核", "中心像素")
        session.record_check(answer)
        checkpoint = session.directory / "results/check_answers.json"
        initial = checkpoint.read_bytes()
        with self.assertRaises(ValueError):
            session.revise_check(changed, 1, " ", "中心像素")
        with self.assertRaises(ValueError):
            session.revise_check(changed, 1, "重新核对", " ")
        with patch("shader_deep.workflows.review.session.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            session.revise_check(changed, 1, "复核", "中心像素")
        self.assertEqual(checkpoint.read_bytes(), initial)
        self.assertEqual(session.check_answers, (answer,))
        self.assertEqual(session.answer_revisions, {self.shape: 1})
        self.assertNotIn("check_revisions", session.execution())
        session.stop("补齐条件")
        with self.assertRaisesRegex(ValueError, "会话已结束"):
            session.revise_check(changed, 1, "复核", "中心像素")
