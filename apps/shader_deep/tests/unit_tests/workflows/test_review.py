"""公开评审入口的固定材料、发布交付与失败证据."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from PIL import Image

from shader_deep.domain.visual_review import DimensionReview, ReviewIssue, ReviewSource, UnassessedItem
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.review import ReviewOptions, run_review
from tests.unit_tests.fixtures._review_fixture import matched_review, review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response

if TYPE_CHECKING:
    import httpx2

    from shader_deep.workflows.review.contracts import ReviewOutcome
    from shader_deep.workflows.review.session import ReviewSession


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.reference, self.rendered = self.directory / "reference.png", self.directory / "rendered.png"
        Image.new("RGB", (8, 6), (20, 90, 150)).save(self.reference)
        Image.new("RGB", (8, 6), (160, 40, 10)).save(self.rendered)
        self.options = ReviewOptions(output_dir=self.directory / "runs", max_request_retries=0)

    def run_case(self, model: ReviewModel | None = None) -> ReviewOutcome:
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model or ReviewModel()):
            return run_review(self.reference, self.rendered, review_request(), options=self.options)

    def test_public_entry_delivers_a_formally_readable_report(self) -> None:
        outcome = self.run_case()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.verdict, "no_major_issue_observed")
        package = read_review_package(outcome.report_dir)
        self.assertEqual(package.report.verdict, outcome.verdict)
        self.assertEqual(package.source.criteria.request, review_request())
        self.assertEqual(outcome.execution["model_calls"], 1)
        self.assertEqual(outcome.execution["cost_status"], "unknown")
        result = json.loads((outcome.run_dir / "result.json").read_text())
        self.assertEqual((result["status"], result["verdict"], result["report_path"]), ("completed", outcome.verdict, "report"))
        self.assertTrue((outcome.run_dir / "results/accepted.json").is_file())

    def test_source_labels_change_the_report_but_not_the_model_messages(self) -> None:
        model = ReviewModel()
        digests = []
        for source in (ReviewSource(), ReviewSource(candidate_id="accepted-baseline", run_id="failed-trial")):
            request = review_request().model_copy(update={"source": source})
            with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
                outcome = run_review(self.reference, self.rendered, request, options=self.options)
            self.assertEqual(outcome.status, "completed", outcome.error)
            package = read_review_package(outcome.report_dir)
            self.assertEqual(package.report.criteria.request.source, source)
            digests.append(package.report.input_sha256)

        self.assertNotEqual(digests[0], digests[1])
        first, second = model.requests
        self.assertEqual([message.content for message in first], [message.content for message in second])

    def test_major_and_inconclusive_reports_are_successful_deliveries(self) -> None:
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                report = matched_review()
                entry = DimensionReview(
                    id="shape",
                    status="uncertain" if uncertain else "major_gap",
                    rationale="证据不足" if uncertain else "轮廓不符",
                    issue_ids=() if uncertain else ("I1",),
                    unassessed_ids=("U1",) if uncertain else (),
                )
                update = {
                    "dimensions": tuple(entry if item.id == "shape" else item for item in report.dimensions),
                    "unassessed": (UnassessedItem(id="U1", dimension="shape", subject="轮廓", reason="当前分辨率不足"),) if uncertain else (),
                    "issues": ()
                    if uncertain
                    else (
                        ReviewIssue(
                            id="I1",
                            dimension="shape",
                            region="中部",
                            reference_observation="圆环开口可见",
                            rendered_observation="开口被填平",
                            severity="major",
                            impact="必要开口缺失",
                        ),
                    ),
                }
                submission = report.model_copy(update=update)
                model = ReviewModel(responses=[review_call("submit_review", {"report": submission.model_dump(mode="json")})])
                outcome = self.run_case(model)
                self.assertEqual(outcome.status, "completed")
                self.assertEqual(outcome.verdict, "inconclusive" if uncertain else "needs_revision")
                self.assertIsNotNone(outcome.report_dir)

    def test_publish_failure_returns_failed_and_preserves_accepted_evidence(self) -> None:
        with patch("shader_deep.workflows.review.entry.write_review_package", side_effect=OSError("disk full")):
            outcome = self.run_case()
        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.report_dir)
        self.assertIsNone(outcome.verdict)
        self.assertIn("publish_error", outcome.error)
        self.assertTrue((outcome.run_dir / "results/accepted.json").is_file())
        self.assertIsNone(json.loads((outcome.run_dir / "result.json").read_text())["report_path"])

    def test_graph_exception_after_acceptance_preserves_delivery(self) -> None:
        def accepted_then_error(session: ReviewSession, _usage: object) -> None:
            session.submit(matched_review(session.source.criteria))
            msg = "post-acceptance failure"
            raise RuntimeError(msg)

        with patch("shader_deep.workflows.review.entry._invoke", side_effect=accepted_then_error):
            outcome = self.run_case()
        self.assertEqual(outcome.status, "completed")
        self.assertIn("post-acceptance failure", outcome.error)
        self.assertIsNotNone(outcome.report_dir)

    def test_model_error_and_stop_have_different_execution_status(self) -> None:
        with patch("shader_deep.workflows.review.entry._invoke", side_effect=RuntimeError("transport unavailable")):
            failed = self.run_case()
        self.assertEqual(failed.status, "failed")
        self.assertIn("transport unavailable", failed.error)
        stopped = self.run_case(ReviewModel(responses=[review_call("stop_review", {"reason": "需要补全比较条件"})]))
        self.assertEqual(stopped.status, "blocked")
        self.assertEqual(stopped.error, "需要补全比较条件")
        self.assertIsNone(stopped.report_dir)
        self.assertIsNone(stopped.verdict)

    def test_input_failure_is_reported_before_building_a_model(self) -> None:
        self.rendered.write_text("not a PNG")
        with patch("shader_deep.workflows.review.entry.build_model") as build, self.assertRaises(ValueError):
            run_review(self.reference, self.rendered, review_request(), options=self.options)
        build.assert_not_called()

    def test_interrupt_preserves_result_index_and_propagates(self) -> None:
        interruption = KeyboardInterrupt()
        with patch("shader_deep.workflows.review.entry._invoke", side_effect=interruption), self.assertRaises(KeyboardInterrupt) as caught:
            self.run_case()
        self.assertIs(caught.exception, interruption)
        result = json.loads(next((self.directory / "runs").glob("run-*/result.json")).read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("中断", result["error"])

    def test_real_transport_payload_contains_both_pngs_and_output_limit(self) -> None:
        requests: list[dict] = []
        report = matched_review().model_dump(mode="json")

        def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(json.loads(request.content))
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "submit", "type": "function", "function": {"name": "submit_review", "arguments": json.dumps({"report": report})}}
                ],
            }
            return _response(request, message, streaming=True)

        with patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.Client.send", autospec=True, side_effect=respond):
            outcome = run_review(self.reference, self.rendered, review_request(), options=self.options)
        self.assertEqual(outcome.status, "completed", outcome.error)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["max_tokens"], 8192)
        self.assertEqual({item["function"]["name"] for item in requests[0]["tools"]}, {"submit_review", "stop_review"})

        def check_schema(node: dict) -> None:
            self.assertNotIn("default", node)
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), expr2=False)
                self.assertCountEqual(node["required"], node["properties"])
                for child in node["properties"].values():
                    check_schema(child)
            if node.get("type") == "array":
                check_schema(node["items"])

        for tool in requests[0]["tools"]:
            self.assertIs(tool["function"]["strict"], expr2=True)
            check_schema(tool["function"]["parameters"])
        image_blocks = [
            block
            for message in requests[0]["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "image_url"
        ]
        self.assertEqual(len(image_blocks), 2)
        self.assertNotEqual(image_blocks[0]["image_url"]["url"], image_blocks[1]["image_url"]["url"])
