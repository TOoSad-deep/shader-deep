"""通过公开入口验证逐项评审、局部纠错和覆盖短路."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image, ImageDraw

from shader_deep.api import (
    DimensionCriteria,
    PixelRegion,
    RequiredRegion,
    ReviewCoverage,
    ReviewOptions,
    ReviewRequest,
    read_review_package,
    run_review,
)
from shader_deep.cli.review import main
from shader_deep.domain.review_items import bound_checks
from tests.unit_tests.fixtures._review_fixture import review_criteria, review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context


def ring_task() -> ReviewRequest:
    dimensions = tuple(
        DimensionCriteria(id="shape", required_checks=("外轮廓", "中心孔"), excluded_checks=("背景网格",)) if item.id == "shape" else item
        for item in review_request().dimensions
    )
    return ReviewRequest(
        protocol="visual_review_v2", scope="local", target_kind="element", target="圆环", requirements="核对轮廓和孔", dimensions=dimensions
    )


def record(check_id: str, rationale: str, call_id: str, *, missing_hole: bool = False) -> AIMessage:
    findings = [{"severity": "major", "region": "中心", "reference_observation": "有孔", "rendered_observation": "实心", "impact": "必需的孔消失"}]
    answer = {"check_id": check_id, "rationale": rationale, "findings": findings if missing_hole else [], "unassessed_reason": None}
    return review_call("record_review_check", {"answer": answer}, call_id)


def finish(call_id: str) -> AIMessage:
    return review_call("finish_review", {"summary": "圆环评审", "limitations": []}, call_id)


def batch(*messages: AIMessage) -> AIMessage:
    return AIMessage(content="", tool_calls=[call for message in messages for call in message.tool_calls])


class ItemReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ref, self.candidate = self.root / "ring.png", self.root / "disk.png"
        with Image.new("RGB", (32, 32), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.ellipse((2, 2, 29, 29), fill="black")
            image.save(self.candidate)
            draw.ellipse((12, 12, 19, 19), fill="white")
            image.save(self.ref)
        self.options = ReviewOptions(output_dir=self.root / "runs", max_request_retries=0)

    def test_missing_answer_requires_repair_and_cannot_erase_other_finding(self) -> None:
        request = ring_task()
        checks = {item.check: item.check_id for item in bound_checks(review_criteria(request), None)}
        hole, rim = checks["中心孔"], checks["外轮廓"]
        model = ReviewModel(
            responses=[
                batch(record(hole, "候选中心被填平", "hole", missing_hole=True), finish("incomplete")),
                batch(record(hole, "中心一致", "overwrite"), record(rim, "外轮廓一致", "rim"), finish("final")),
            ]
        )
        with patch("shader_deep.workflows.review.entry.build_model", return_value=model):
            result = run_review(self.ref, self.candidate, request, options=self.options)
        self.assertEqual((result.status, result.verdict), ("completed", "needs_revision"), result.error)
        self.assertIsNotNone(result.report_dir)
        package = read_review_package(result.report_dir)
        self.assertEqual(package.manifest.schema_version, "visual_review_v2")
        self.assertEqual({answer.check_id for answer in package.report.check_answers}, {hole, rim})
        self.assertEqual([item.check for item in package.report.submission.issues], ["中心孔"])
        self.assertEqual(next(item.status for item in package.report.submission.dimensions if item.id == "shape"), "major_gap")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(review_context(model.requests[1])["answered_check_ids"], [hole])
        error = next(item for item in model.requests[1] if isinstance(item, ToolMessage) and item.tool_call_id == "incomplete")
        self.assertIn(rim, str(error.content))
        self.assertEqual(json.loads((result.run_dir / "result.json").read_bytes())["review_protocol"], "visual_review_v2")

    def test_cli_keeps_visible_hole_finding_and_locked_rim_gap(self) -> None:
        request = ring_task()
        region = PixelRegion(left=0, top=0, right=32, bottom=32)
        cropped = PixelRegion(left=8, top=8, right=24, bottom=24)
        coverage = ReviewCoverage(
            reference_window=region, rendered_window=cropped, required_regions=(RequiredRegion(dimension="shape", check="外轮廓", region=region),)
        )
        request = request.model_copy(update={"coverage": coverage})
        with Image.open(self.candidate) as image, image.crop((8, 8, 24, 24)) as view:
            view.save(self.root / "center.png")
        task_file = self.root / "task.json"
        task_file.write_text(request.model_dump_json(), encoding="utf-8")
        checks = {item.check: item.check_id for item in bound_checks(review_criteria(request), None)}
        hole_evidence = record(checks["中心孔"], "中心被填平", "visible", missing_hole=True).tool_calls[0]["args"]["answer"]
        correction = review_call(
            "revise_review_check",
            {"answer": hole_evidence, "expected_revision": 1, "reason": "初次漏看了中心", "evidence": "参考中央是白孔, 候选中央采集区是连续的黑色"},
            "revised",
        )
        model = ReviewModel(
            responses=[
                batch(
                    record(checks["外轮廓"], "窗口内轮廓一致", "locked"),
                    record(checks["中心孔"], "初看一致", "initial"),
                    correction,
                    finish("done"),
                ),
            ]
        )
        options = ["review", str(self.ref), str(self.root / "center.png"), "--task", str(task_file), "--output-dir", str(self.root / "runs")]
        output = io.StringIO()
        with (
            patch("sys.argv", options),
            patch("shader_deep.workflows.review.entry.build_model", return_value=model),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        index = json.loads(output.getvalue())
        package = read_review_package(Path(index["report_dir"]))
        report = package.report
        self.assertEqual(report.verdict, "needs_revision")
        self.assertEqual([answer.check_id for answer in report.check_answers], [checks["中心孔"]])
        self.assertEqual([item.subject for item in report.submission.unassessed], ["外轮廓"])
        evidence_message = review_context(model.requests[0])["evidence"]
        self.assertEqual(evidence_message["coverage"], report.evidence.coverage.model_dump(mode="json"))
        self.assertEqual(evidence_message["samples"], [])
        self.assertNotIn("units", evidence_message)
        self.assertEqual([item.check for item in report.submission.issues], ["中心孔"])
        self.assertEqual(report.metadata["validation_errors"], 1)
        self.assertEqual(index["action_summary"], report.action_summary.model_dump(mode="json"))
        self.assertEqual([issue.check for issue in report.action_summary.major_issues], ["中心孔"])
        self.assertEqual([gap.subject for gap in report.action_summary.unassessed], ["外轮廓"])
        self.assertEqual(report.metadata["check_revisions"][0]["before"]["findings"], [])
        recovered = io.StringIO()
        with (
            patch("sys.argv", ["review", "--republish", index["run_dir"]]),
            patch("shader_deep.workflows.review.entry.build_model") as unused_model,
            redirect_stdout(recovered),
        ):
            self.assertEqual(main(), 0)
        unused_model.assert_not_called()
        self.assertEqual(json.loads(recovered.getvalue())["action_summary"], index["action_summary"])
        self.assertEqual(read_review_package(package.directory).content_sha256, package.content_sha256)

    def test_all_checks_outside_capture_have_formal_report_without_model(self) -> None:
        window = PixelRegion(left=0, top=0, right=32, bottom=32)
        absent = PixelRegion(left=32, top=0, right=64, bottom=32)
        coverage = ReviewCoverage(
            reference_window=window,
            rendered_window=window,
            required_regions=tuple(RequiredRegion(dimension="shape", check=text, region=absent) for text in ("外轮廓", "中心孔")),
        )
        request = ring_task().model_copy(update={"coverage": coverage})
        with patch("shader_deep.workflows.review.entry.build_model") as build:
            result = run_review(self.ref, self.candidate, request, options=self.options)
        build.assert_not_called()
        self.assertEqual((result.status, result.verdict), ("completed", "inconclusive"))
        package = read_review_package(result.report_dir)
        self.assertEqual(package.report.check_answers, ())
        self.assertEqual({item.subject for item in package.report.submission.unassessed}, {"中心孔", "外轮廓"})
