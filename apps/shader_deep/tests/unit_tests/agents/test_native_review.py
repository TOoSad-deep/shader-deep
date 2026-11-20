"""无网络模型通过真实 Deep Agents 图验证独立评审权限和终态."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image

from shader_deep.agents.review.native import build_review_agent, review_input
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.review_package import capture_review_source
from shader_deep.workflows.review.options import ReviewOptions
from shader_deep.workflows.review.session import ReviewSession
from tests.unit_tests.fixtures._review_fixture import matched_review, review_request
from tests.unit_tests.fixtures._review_model import ReviewModel, review_call, review_context
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import DETAIL, _response

if TYPE_CHECKING:
    import httpx2

    from shader_deep.domain.visual_review import ReviewRequest


class NativeReviewTests(unittest.TestCase):
    def make_session(self, *, max_model_calls: int = 3, regions: bool = False, request: ReviewRequest | None = None) -> ReviewSession:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reference, rendered = directory / "reference.png", directory / "rendered.png"
        Image.new("RGB", (8, 6), (20, 90, 150)).save(reference)
        Image.new("RGB", (8, 6), (160, 40, 10)).save(rendered)
        request = request or review_request()
        if regions:
            request = request.model_copy(
                update={
                    "scope": "local",
                    "target_kind": "feature",
                    "reference_region": PixelRegion(left=1, top=1, right=6, bottom=5),
                    "rendered_region": PixelRegion(left=1, top=1, right=6, bottom=5),
                }
            )
        source = capture_review_source(reference, rendered, directory / "run", request)
        return ReviewSession(source, ReviewOptions(max_model_calls=max_model_calls, max_request_retries=0))

    def invoke(self, model: ReviewModel, session: ReviewSession) -> dict[str, object]:
        return build_review_agent(model, session, session.options).invoke(review_input(session))

    def test_real_requests_contain_fixed_labeled_images_and_only_two_tools(self) -> None:
        session = self.make_session(regions=True)
        model = ReviewModel()
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(set(model.offered_tools), {"submit_review", "stop_review"})
        payload = review_context(model.requests[0])
        criteria = payload["criteria"]
        self.assertEqual(criteria["profile_id"], "feature")
        self.assertEqual(criteria["criteria_sha256"], session.source.criteria.criteria_sha256)
        self.assertEqual(criteria["request"], session.source.request.model_dump(mode="json", exclude={"source"}))
        self.assertNotIn("code", payload)
        self.assertNotIn("assessment", payload)
        blocks = [block for message in model.requests[0] if isinstance(message.content, list) for block in message.content if isinstance(block, dict)]
        urls = [block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"]
        self.assertEqual(urls, [url for _label, url in session.source.images])
        self.assertNotEqual(base64.b64decode(urls[0].split(",", 1)[1]), base64.b64decode(urls[1].split(",", 1)[1]))
        for label, _url in session.source.images:
            self.assertIn(label, str(blocks))
        self.assertTrue((session.directory / "results/accepted.json").is_file())

    def test_domain_error_can_be_corrected_in_the_next_request(self) -> None:
        session = self.make_session()
        invalid = matched_review(session.source.criteria).model_dump(mode="json")
        invalid["dimensions"][2]["status"] = "major_gap"
        model = ReviewModel(responses=[review_call("submit_review", {"report": invalid})])
        result = self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.model_calls, 2)
        self.assertEqual(session.validation_errors, 1)
        self.assertTrue(any(isinstance(item, ToolMessage) and item.status == "error" for item in result["messages"]))
        self.assertEqual(review_context(model.requests[0])["criteria"], review_context(model.requests[1])["criteria"])

    def test_guessed_default_tools_are_denied_without_side_effects(self) -> None:
        session = self.make_session(max_model_calls=4)
        model = ReviewModel(
            responses=[
                review_call("task", {"subagent_type": "general-purpose", "description": "delegate"}),
                review_call("write_file", {"file_path": "/unexpected.txt", "content": "changed"}, "write"),
                review_call("read_file", {"file_path": "/etc/passwd"}, "read"),
            ]
        )
        result = self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.tool_refusals, 3)
        self.assertEqual(len([item for item in result["messages"] if isinstance(item, ToolMessage) and item.status == "error"]), 3)
        self.assertNotIn("/unexpected.txt", result.get("files", {}))

    def test_first_terminal_in_batch_wins_for_sync_and_async(self) -> None:
        for asynchronous in (False, True):
            for first in ("submit_review", "stop_review"):
                with self.subTest(asynchronous=asynchronous, first=first):
                    session = self.make_session()
                    report = matched_review(session.source.criteria).model_dump(mode="json")
                    first_args = {"report": report} if first == "submit_review" else {"reason": "需补充背景"}
                    batch = AIMessage(
                        content="",
                        tool_calls=[
                            {"name": first, "args": first_args, "id": "first"},
                            {"name": "submit_review", "args": {"report": {**report, "summary": "不可覆盖"}}, "id": "second"},
                        ],
                    )
                    model = ReviewModel(responses=[batch])
                    graph = build_review_agent(model, session, session.options)
                    result = asyncio.run(graph.ainvoke(review_input(session))) if asynchronous else graph.invoke(review_input(session))
                    self.assertEqual(len(model.requests), 1)
                    tools = [item for item in result["messages"] if isinstance(item, ToolMessage)]
                    self.assertEqual([item.status for item in tools], ["success", "error"])
                    if first == "submit_review":
                        self.assertEqual(session.submission.summary, report["summary"])
                    else:
                        self.assertIsNone(session.submission)
                        self.assertEqual(session.finalize().status, "blocked")

    def test_v2_checks_can_be_batched_and_finish_in_one_model_call(self) -> None:
        request = review_request(required=("shape", "color")).model_copy(update={"protocol": "visual_review_v2"})
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                session = self.make_session(max_model_calls=1, request=request)
                calls = [
                    {
                        "name": "record_review_check",
                        "args": {"answer": {"check_id": item.check_id, "rationale": "可见区域符合要求"}},
                        "id": item.check_id,
                    }
                    for item in reversed(session.checks)
                ]
                calls.insert(1, {"name": "submit_review", "args": {"report": {}}, "id": "old-protocol"})
                calls.append({"name": "finish_review", "args": {"summary": "两项已核对"}, "id": "finish"})
                model = ReviewModel(responses=[AIMessage(content="", tool_calls=calls)])
                graph = build_review_agent(model, session, session.options)
                result = asyncio.run(graph.ainvoke(review_input(session))) if asynchronous else graph.invoke(review_input(session))
                self.assertEqual(session.finalize().status, "completed")
                self.assertEqual(
                    {tool.name for tool in result["messages"] if isinstance(tool, ToolMessage) and tool.status == "success"},
                    {"record_review_check", "finish_review"},
                )
                self.assertEqual(session.tool_refusals, 1)
                self.assertCountEqual(model.offered_tools, ["record_review_check", "revise_review_check", "finish_review", "stop_review"])
                self.assertEqual(session.model_calls, 1)
                context = review_context(model.requests[0])
                check_ids = [check.check_id for check in session.checks]
                self.assertEqual([item["check_id"] for item in context["answerable_checks"]], check_ids)
                self.assertEqual(context["answered_check_ids"], [])
                self.assertEqual(context["final_call"]["pending_check_ids"], check_ids)
                self.assertIn("finish_review", context["final_call"]["instructions"])
                accepted = json.loads((session.directory / "results/accepted.json").read_text())
                self.assertEqual([item["check_id"] for item in accepted["check_answers"]], check_ids)

    def test_v2_last_call_identifies_pending_checks_and_still_requires_explicit_finish(self) -> None:
        request = review_request(required=("shape", "color")).model_copy(update={"protocol": "visual_review_v2"})
        session = self.make_session(max_model_calls=2, request=request)
        first, second = session.checks
        responses = [
            review_call("record_review_check", {"answer": {"check_id": item.check_id, "rationale": "可见区域符合要求"}}, item.check_id)
            for item in (first, second)
        ]
        model = ReviewModel(responses=responses)
        self.invoke(model, session)
        initial, last = [review_context(messages) for messages in model.requests]
        self.assertNotIn("final_call", initial)
        self.assertEqual(last["model_calls_remaining"], 0)
        self.assertEqual(last["final_call"]["pending_check_ids"], [second.check_id])
        self.assertEqual(last["final_call"]["answer_id_fields"], [{"check_id": second.check_id}])
        self.assertEqual([answer.check_id for answer in session.check_answers], [first.check_id, second.check_id])
        outcome = session.finalize()
        self.assertEqual((outcome.status, outcome.error, outcome.submission), ("blocked", "model_call_limit", None))

    def test_v2_cannot_register_an_answer_if_its_snapshot_write_fails(self) -> None:
        request = review_request().model_copy(update={"protocol": "visual_review_v2"})
        session = self.make_session(request=request)
        answer = {"check_id": session.checks[0].check_id, "rationale": "轮廓吻合"}
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": "record_review_check", "args": {"answer": answer}, "id": "shape"},
                {"name": "finish_review", "args": {"summary": "轮廓吻合"}, "id": "finish"},
            ],
        )
        with patch("shader_deep.workflows.review.session.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.invoke(ReviewModel(responses=[message]), session)
        self.assertEqual(session.check_answers, ())
        self.assertIsNone(session.submission)
        self.assertFalse((session.directory / "results/accepted.json").exists())

    def test_invalid_submissions_obey_budget_and_plain_text_is_no_submission(self) -> None:
        session = self.make_session(max_model_calls=2)
        bad = review_call("submit_review", {"report": {}}, "bad")
        model = ReviewModel(responses=[bad, bad])
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "blocked")
        self.assertEqual(session.reason, "model_call_limit")
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn("final_call", review_context(model.requests[-1]))
        another = self.make_session()
        self.invoke(ReviewModel(responses=[AIMessage(content="已评审")]), another)
        self.assertEqual(another.finalize().error, "no_submission")

    def test_acceptance_write_failure_does_not_accept_or_run_later_tools(self) -> None:
        session = self.make_session()
        report = matched_review(session.source.criteria).model_dump(mode="json")
        batch = AIMessage(
            content="",
            tool_calls=[
                {"name": "submit_review", "args": {"report": report}, "id": "submit"},
                {"name": "stop_review", "args": {"reason": "later"}, "id": "stop"},
            ],
        )
        with patch("shader_deep.workflows.review.session.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.invoke(ReviewModel(responses=[batch]), session)
        self.assertIsNone(session.submission)
        self.assertEqual(session.state, "running")

    def test_async_provider_stream_preserves_raw_arguments_reasoning_and_finish(self) -> None:
        session = self.make_session()
        report = matched_review(session.source.criteria).model_dump(mode="json")
        invalid = matched_review(session.source.criteria).model_dump(mode="json")
        invalid["dimensions"][2]["status"] = "major_gap"
        requests: list[dict] = []

        async def respond(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(json.loads(request.content))
            current = invalid if len(requests) == 1 else report
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_details": [DETAIL],
                "tool_calls": [
                    {
                        "id": f"submit-{len(requests)}",
                        "type": "function",
                        "function": {"name": "submit_review", "arguments": json.dumps({"report": current})},
                    }
                ],
            }
            return _response(request, message, streaming=True)

        with patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=respond):
            graph = build_review_agent(build_model(), session, session.options)
            result = asyncio.run(graph.ainvoke(review_input(session)))
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.validation_errors, 1)
        self.assertEqual(len(requests), 2)
        assistant = next(item for item in requests[1]["messages"] if item.get("role") == "assistant")
        self.assertEqual(assistant["reasoning_details"], [DETAIL])
        for message in result["messages"]:
            if isinstance(message, AIMessage):
                self.assertEqual(message.response_metadata["finish_reason"], "tool_calls")
        self.assertEqual(requests[0]["max_tokens"], session.options.max_output_tokens)

    def test_async_provider_truncated_json_cannot_execute_a_tool(self) -> None:
        session = self.make_session()

        async def respond(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "submit", "type": "function", "function": {"name": "submit_review", "arguments": '{"report":'}}],
            }
            return _response(request, message, streaming=True)

        with (
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.AsyncClient.send", autospec=True, side_effect=respond),
            self.assertRaises(ValueError),
        ):
            graph = build_review_agent(build_model(), session, session.options)
            asyncio.run(graph.ainvoke(review_input(session)))
        self.assertIsNone(session.submission)
        self.assertFalse((session.directory / "results/accepted.json").exists())
