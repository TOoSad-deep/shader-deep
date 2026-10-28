"""通过真实 SDK 编解码验证 OpenRouter 多轮图像、工具与推理块载荷."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

import httpx2
from langchain.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from shader_deep.domain.five_libraries import Element, ExplorationSubmission, validate_exploration
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.openrouter import ReasoningBuffer, StreamTerminal
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.infrastructure.test_config import OPENROUTER

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage

DETAIL = {"type": "reasoning.text", "id": "reason-1", "format": "glm", "index": 0, "text": "Inspect image."}


def _response(request: httpx2.Request, message: dict, *, streaming: bool) -> httpx2.Response:
    base = {"id": "router-response", "created": 0, "model": "openrouter-fixture"}
    reason = "tool_calls" if message.get("tool_calls") else "stop"
    if not streaming:
        return httpx2.Response(
            200, request=request, json={**base, "object": "chat.completion", "choices": [{"index": 0, "message": message, "finish_reason": reason}]}
        )
    delta = {**message, "tool_calls": [{**call, "index": index} for index, call in enumerate(message.get("tool_calls", []))]}
    delta.pop("reasoning_details", None)
    deltas = [{"reasoning_details": [{**DETAIL, "text": text}]} for text in ("Inspect ", "image.")] if "reasoning_details" in message else []
    frames = [
        {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": part, "finish_reason": None}]} for part in [*deltas, delta]
    ]
    frames.append({**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
    # OpenRouter 实测会重复发送正常结束帧, SDK 不能把标记拼成 tool_callstool_calls.
    frames.append({**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    return httpx2.Response(200, request=request, content=body.encode(), headers={"content-type": "text/event-stream"})


class OpenRouterTransportTests(TestCase):
    def test_five_library_text_survives_outbound_schema_and_local_validation(self) -> None:
        target = Element(id="E1", name="霓虹圆角边框", region="图像中央", feature_ids=())

        @tool
        def submit_exploration(report: ExplorationSubmission) -> str:
            """校验并接收探索报告."""
            validate_exploration(report, target)
            return "accepted"

        report = {
            "features": [{"id": "F1", "description": "连续发光轮廓", "mechanism_refs": [{"id": "M1"}]}],
            "relations": [],
            "mechanisms": [{"id": "M1", "name": "距离场遮罩", "method": "计算到圆角矩形边界的距离"}],
            "sketches": [{"id": "S1", "element_id": "E1", "name": "轮廓方案", "default": {"F1": ["M1"]}, "composition": "使用 [[M1]] 实现 [[F1]]"}],
        }
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-report", "type": "function", "function": {"name": "submit_exploration", "arguments": json.dumps({"report": report})}}
            ],
        }
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                payload, response = self._schema_request(submit_exploration, message, streaming=streaming)
                parameters = payload["tools"][0]["function"]["parameters"]
                self.assertNotIn('"pattern": "\\\\S"', json.dumps(parameters))
                sketch = parameters["properties"]["report"]["properties"]["sketches"]["items"]["properties"]
                self.assertEqual(sketch["default"]["additionalProperties"]["items"]["minLength"], 1)
                self.assertEqual(response.tool_calls[0]["args"], {"report": report})
                self.assertEqual(submit_exploration.invoke(response.tool_calls[0]["args"]), "accepted")
        for blank in ("", " ", "\t\n", "\u3000"):
            invalid = deepcopy(report)
            invalid["features"][0]["description"] = blank
            with self.subTest(blank=blank), self.assertRaises(ValidationError):
                submit_exploration.invoke({"report": invalid})

    def _schema_request(
        self, tool_: object, message: dict, *, streaming: bool = False, base_url: str = "https://openrouter.ai/api/v1"
    ) -> tuple[dict, AIMessage]:
        requests = []

        def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(json.loads(request.content))
            return _response(request, message, streaming=streaming)

        with (
            patch.dict(os.environ, {**OPENROUTER, "OPENROUTER_BASE_URL": base_url}, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=respond),
        ):
            model = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=streaming)).bind_tools([tool_])
            response = model.invoke([HumanMessage(content="提交完整报告")])
        return requests[0], response

    def test_schema_adaptation_preserves_other_patterns_literals_and_provider_boundaries(self) -> None:
        text = {"type": "string", "minLength": 1, "pattern": r"\S"}
        literal = {"type": "string", "pattern": r"\S"}
        parameters = {
            "type": "object",
            "required": ["label"],
            "properties": {
                "label": {**text, "default": literal, "examples": [literal]},
                "code": {"type": "string", "pattern": "^M[0-9]+$"},
                "optional": {"anyOf": [text, {"type": "null"}]},
                "choices": {"type": "object", "additionalProperties": {"type": "array", "items": text}},
            },
        }
        tool_ = {"type": "function", "function": {"name": "probe", "description": "Schema compatibility", "parameters": parameters}}
        original = deepcopy(tool_)
        for base_url, expected in (("https://openrouter.ai/api/v1", False), ("https://other-provider.invalid/v1", True)):
            with self.subTest(base_url=base_url):
                payload, _ = self._schema_request(tool_, {"role": "assistant", "content": "done"}, base_url=base_url)
                schema = payload["tools"][0]["function"]["parameters"]
                properties = schema["properties"]
                for field in (properties["label"], properties["optional"]["anyOf"][0], properties["choices"]["additionalProperties"]["items"]):
                    self.assertEqual("pattern" in field, expected)
                    self.assertEqual(field["minLength"], 1)
                self.assertEqual(properties["label"]["default"], literal)
                self.assertEqual(properties["label"]["examples"], [literal])
                self.assertEqual(properties["code"]["pattern"], "^M[0-9]+$")
                self.assertEqual(schema["required"], ["label"])
                self.assertEqual(tool_, original)

    def test_image_tool_and_reasoning_round_trip_in_both_modes(self) -> None:
        @tool
        def inspect_image() -> str:
            """确认已读取参考图."""
            return "ok"

        message = {
            "role": "assistant",
            "content": "",
            "reasoning_details": [DETAIL],
            "tool_calls": [{"id": "call-image", "type": "function", "function": {"name": "inspect_image", "arguments": "{}"}}],
        }
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self._round_trip(inspect_image, message, streaming=streaming)

    def _round_trip(self, tool_: object, message: dict, *, streaming: bool) -> None:
        requests = []

        def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
            payload = json.loads(request.content)
            requests.append(payload)
            response = message if len(requests) == 1 else {"role": "assistant", "content": "done"}
            return _response(request, response, streaming=streaming)

        with (
            patch.dict(os.environ, {**OPENROUTER, "OPENROUTER_REASONING_EFFORT": "low", "OPENROUTER_PROVIDER_SORT": "throughput"}, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=respond),
        ):
            model = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=streaming, max_output_tokens=4096)).bind_tools(
                [tool_]
            )
            reference = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}])
            first = model.invoke([reference])
            self.assertEqual(first.response_metadata["finish_reason"], "tool_calls")
            self.assertEqual(first.additional_kwargs["reasoning_details"], [DETAIL])
            self.assertEqual(first.tool_calls[0]["args"], {})
            self.assertEqual(model.invoke([reference, first, ToolMessage(content="ok", tool_call_id="call-image")]).content, "done")
        self.assertEqual(requests[1]["messages"][1]["reasoning_details"], [DETAIL])
        self.assertEqual(requests[0]["messages"][0]["content"], reference.content)
        self.assertEqual(requests[0]["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", requests[0])
        self.assertEqual(requests[0]["provider"], {"require_parameters": True, "sort": "throughput"})
        self.assertEqual(requests[0]["reasoning"], {"effort": "low"})
        self.assertTrue(requests[0]["tools"][0]["function"]["strict"])

    def test_encrypted_block_and_summary_keep_separate_identities(self) -> None:
        buffer = ReasoningBuffer()
        buffer.append([{"index": 1, "type": "reasoning.encrypted", "data": "opaque", "format": "glm"}])
        buffer.append([{"index": 0, "type": "reasoning.summary", "summary": "first "}])
        buffer.append([{"index": 0, "summary": "second"}])
        self.assertEqual(buffer.details()[0]["summary"], "first second")
        self.assertEqual(buffer.details()[1]["data"], "opaque")

    def test_changed_block_identity_is_rejected(self) -> None:
        buffer = ReasoningBuffer()
        buffer.append([DETAIL])
        with self.assertRaisesRegex(ValueError, "stable field: id"):
            buffer.append([{**DETAIL, "id": "different"}])

    def test_conflicting_finish_markers_are_rejected(self) -> None:
        terminal = StreamTerminal()
        self.assertTrue(terminal.accept({"finish_reason": "tool_calls"}))
        with self.assertRaisesRegex(ValueError, "conflicting finish"):
            terminal.accept({"finish_reason": "length"})
