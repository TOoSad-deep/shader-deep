"""通过真实 SDK 编解码验证 OpenRouter 多轮图像、工具与推理块载荷."""

from __future__ import annotations

import json
import os
from unittest import TestCase
from unittest.mock import patch

import httpx2
from langchain.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage

from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.openrouter import ReasoningBuffer, StreamTerminal
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.infrastructure.test_config import OPENROUTER

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
