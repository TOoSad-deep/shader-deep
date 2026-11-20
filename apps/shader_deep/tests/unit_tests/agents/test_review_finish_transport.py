"""通过实际 OpenRouter 请求编码与流式解码, 验证 V2 可在一次工具调用中提交全部答案."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

from PIL import Image, ImageDraw

from shader_deep.api import ReviewOptions, read_review_package, run_review
from tests.unit_tests.fixtures._review_fixture import review_criteria
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response
from tests.unit_tests.workflows.test_review_items import ring_task

if TYPE_CHECKING:
    import httpx2


class FinishTransportTests(TestCase):
    def test_streamed_finish_review_with_two_answers_delivers_in_one_call(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reference, candidate = root / "ring.png", root / "disk.png"
        with Image.new("RGB", (16, 16), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.ellipse((1, 1, 14, 14), fill="black")
            image.save(candidate)
            draw.ellipse((5, 5, 10, 10), fill="white")
            image.save(reference)
        request = ring_task()
        checks = {check.check: check.check_id for check in review_criteria(request).checks}
        hole = {
            "region": "中心",
            "reference_observation": "白色圆孔",
            "rendered_observation": "黑色实心",
            "severity": "major",
            "impact": "中心孔消失",
        }
        answers = [
            {"check_id": checks["中心孔"], "rationale": "中心孔被填平", "findings": [hole], "unassessed_reason": None},
            {"check_id": checks["外轮廓"], "rationale": "两图外圈闭合", "findings": [], "unassessed_reason": None},
        ]
        arguments = {"answers": answers, "summary": "中心孔消失, 外轮廓保留", "limitations": []}
        payloads = []

        def respond(_client: httpx2.Client, outgoing: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            payloads.append(json.loads(outgoing.content))
            call = {"id": "final", "type": "function", "function": {"name": "finish_review", "arguments": json.dumps(arguments)}}
            return _response(outgoing, {"role": "assistant", "content": "", "tool_calls": [call]}, streaming=True)

        options = ReviewOptions(output_dir=root / "runs", max_model_calls=1, max_request_retries=0, stream_model_responses=True)
        with patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.Client.send", autospec=True, side_effect=respond):
            result = run_review(reference, candidate, request, options=options)

        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["stream"])
        finish = next(tool["function"] for tool in payloads[0]["tools"] if tool["function"]["name"] == "finish_review")
        self.assertIs(finish["strict"], expr2=True)
        schema = finish["parameters"]
        submitted = schema["properties"]["answers"]
        self.assertEqual(submitted["type"], "array")
        answer = submitted["items"]
        finding = answer["properties"]["findings"]["items"]
        for obj in (schema, answer, finding):
            self.assertIs(obj["additionalProperties"], expr2=False)
            self.assertCountEqual(obj["required"], obj["properties"])
        self.assertEqual((result.status, result.verdict), ("completed", "needs_revision"), result.error)
        self.assertEqual((result.execution["model_calls"], result.execution["validation_errors"]), (1, 0))
        report = read_review_package(result.report_dir).report
        self.assertCountEqual([item.model_dump(mode="json") for item in report.check_answers], answers)
        self.assertEqual([(issue.check, issue.severity) for issue in report.submission.issues], [("中心孔", "major")])
