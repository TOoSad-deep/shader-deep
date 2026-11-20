"""从公开评审入口验证 V2 拒绝诊断的内容、替换和隐私边界."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from PIL import Image

from shader_deep.api import ReviewOptions, read_review_package, run_review
from shader_deep.domain.review_items import bound_checks
from tests.unit_tests.fixtures._review_fixture import matched_review, review_criteria, review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage

    from shader_deep.domain.visual_review import ReviewRequest
    from shader_deep.workflows.review.contracts import ReviewOutcome


class RejectionDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.image = self.root / "image.png"
        with Image.new("RGB", (4, 4), "white") as source:
            source.save(self.image)
        self.request = review_request().model_copy(update={"protocol": "visual_review_v2"})
        self.check_id = bound_checks(review_criteria(self.request), None)[0].check_id

    def run_responses(self, request: ReviewRequest, *responses: AIMessage) -> ReviewOutcome:
        model = ReviewModel(responses=list(responses))
        options = ReviewOptions(max_model_calls=len(responses), max_request_retries=0, output_dir=self.root / "runs")
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            return run_review(self.image, self.image, request, options=options)

    def test_last_contract_rejection_points_to_remaining_id_and_round(self) -> None:
        unknown = review_call("record_review_check", {"answer": {"check_id": "用户私有标签", "rationale": "任意自由文本"}}, "earlier")
        finish = review_call("finish_review", {"summary": "私有摘要"}, "later")
        result = self.run_responses(self.request, unknown, finish)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.execution["validation_errors"], 2)
        path = result.run_dir / "results" / "last_rejection.json"
        diagnostic = json.loads(path.read_bytes())
        self.assertEqual(
            (diagnostic["tool"], diagnostic["model_call"], diagnostic["code"], diagnostic["path"]),
            ("finish_review", 2, "missing_answers", "/answers"),
        )
        self.assertEqual((diagnostic["missing_count"], diagnostic["missing_check_ids"]), (1, [self.check_id]))
        self.assertNotIn("私有", path.read_text())
        self.assertFalse(path.with_suffix(".tmp").exists())

    def test_schema_error_reports_nested_field_without_copying_input_or_sdk_error(self) -> None:
        private = "data:image/png;base64," + "sensitive-raw-content" * 4096
        finding = {"region": private, "reference_observation": private, "rendered_observation": private, "severity": private, "impact": private}
        answer = {"check_id": self.check_id, "rationale": private, "findings": [finding]}
        result = self.run_responses(self.request, review_call("record_review_check", {"answer": answer}, "request-with-private-id"))
        self.assertEqual(result.status, "blocked")
        content = (result.run_dir / "results" / "last_rejection.json").read_bytes()
        diagnostic = json.loads(content)
        self.assertEqual(diagnostic["code"], "invalid_tool_arguments")
        self.assertEqual(diagnostic["path"], "/arguments/answer/findings/0/severity")
        self.assertEqual(diagnostic["schema_error_type"], "literal_error")
        self.assertEqual(diagnostic["arguments"]["check_id"], self.check_id)
        self.assertEqual(diagnostic["arguments"]["answer_fields"]["rationale"]["length"], len(private))
        self.assertLess(len(content), 2048)
        for fragment in (b"data:image/", b"sensitive", b"private-id"):
            self.assertNotIn(fragment, content)

    def test_finish_id_rejection_distinguishes_missing_invalid_type_and_blank_at_same_path(self) -> None:
        for fields, reason in (
            ({}, "missing"),
            ({"check_id": None}, "string_type"),
            ({"check_id": 8}, "string_type"),
            ({"check_id": " "}, "string_pattern_mismatch"),
        ):
            with self.subTest(fields=fields):
                answer = {"rationale": "private observation", **fields}
                call = review_call("finish_review", {"answers": [answer], "summary": "private summary"})
                outcome = self.run_responses(self.request, call)
                content = (outcome.run_dir / "results" / "last_rejection.json").read_bytes()
                diagnostic = json.loads(content)
                self.assertEqual((diagnostic["path"], diagnostic["schema_error_type"]), ("/arguments/answers/0/check_id", reason))
                self.assertNotIn(b"private", content)

    def test_partial_finish_diagnostic_reports_only_omitted_ids_without_saving_answers(self) -> None:
        req = review_request(required=("shape", "color")).model_copy(update={"protocol": "visual_review_v2"})
        sent, omitted = bound_checks(review_criteria(req), None)
        msg = review_call("finish_review", {"summary": "完成", "answers": [{"check_id": sent.check_id, "rationale": "私有观察"}]})
        result = self.run_responses(req, msg)
        self.assertEqual(result.status, "blocked")
        path = result.run_dir / "results"
        diagnostic = json.loads((path / "last_rejection.json").read_bytes())
        self.assertEqual(diagnostic["code"], "missing_answers")
        self.assertEqual((diagnostic["missing_count"], diagnostic["missing_check_ids"]), (1, [omitted.check_id]))
        self.assertEqual(diagnostic["arguments"]["fields"]["answers"], {"type": "list", "length": 1})
        self.assertNotIn("私有观察", (path / "last_rejection.json").read_text())
        self.assertFalse((path / "check_answers.json").exists())

    def test_unknown_check_id_is_not_persisted_even_in_domain_error_path(self) -> None:
        private = "private-credential-or-label"
        result = self.run_responses(self.request, review_call("record_review_check", {"answer": {"check_id": private, "rationale": private}}))
        data = (result.run_dir / "results" / "last_rejection.json").read_text()
        diagnostic = json.loads(data)
        self.assertEqual((diagnostic["code"], diagnostic["path"]), ("unknown_check", "/answers"))
        self.assertNotIn(private, data)

    def test_no_file_for_direct_stop_or_v1_rejection(self) -> None:
        stopped = self.run_responses(self.request, review_call("stop_review", {"reason": "视角冲突"}))
        self.assertEqual(stopped.status, "blocked")
        self.assertFalse((stopped.run_dir / "results" / "last_rejection.json").exists())
        submitted = {"report": matched_review().model_dump(mode="json")}
        v1 = self.run_responses(review_request(), review_call("submit_review", {"report": {}}), review_call("submit_review", submitted))
        self.assertEqual(read_review_package(v1.report_dir).report.verdict, "no_major_issue_observed")
        self.assertEqual(v1.execution["validation_errors"], 1)
        self.assertFalse((v1.run_dir / "results" / "last_rejection.json").exists())
