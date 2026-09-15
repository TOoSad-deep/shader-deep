"""生成闭环的模型传输夹具, 模型响应可模拟而渲染器可独立选择."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

import httpx2

from shader_deep.config import GenerationOptions
from tests.unit_tests.test_context import BASE_CODE, fixture_state

if TYPE_CHECKING:
    from collections.abc import Callable


def task_payloads(request: dict[str, object]) -> list[dict[str, object]]:
    payloads = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, list):
            payloads.extend(
                json.loads(block["text"])
                for block in content
                if block.get("type") == "text" and block["text"].startswith('{\n  "kind": "generation_task_context"')
            )
    return payloads


def tool_results(request: dict[str, object]) -> list[dict[str, object]]:
    results = []
    for message in request["messages"]:
        if message["role"] == "tool":
            try:
                results.append(json.loads(message["content"]))
            except json.JSONDecodeError:
                results.append({"status": "tool_error", "message": message["content"]})
    return results


class GenerationFixture(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = fixture_state(self.root)
        self.options = GenerationOptions(output_dir=self.root / "runs", width=32, height=24, max_attempts=2)
        self.requests: list[dict[str, object]] = []
        self.response: Callable[[dict[str, object]], dict[str, object]] = self.render_then_finish
        environment = patch.dict(
            os.environ,
            {
                "MICU_MODEL": "fixture-model",
                "MICU_BASE_URL": "https://shader-deep.invalid/v1",
                "MICU_API_KEY": "fixture-key",
                "DS_MICU_MODEL": "",
                "DS_MICU_BASE_URL": "",
                "DS_MICU_API_KEY": "",
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch("httpx2.Client.send", autospec=True, side_effect=self.respond)
        transport.start()
        self.addCleanup(transport.stop)

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{len(self.requests)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    def render_then_finish(self, request: dict[str, object]) -> dict[str, object]:
        rendered = [result for result in tool_results(request) if result["status"] == "rendered"]
        if rendered:
            return self.call("finish_shader", {"candidate_id": rendered[-1]["candidate_id"], "assessment": "已查看预览, 仍待用户验收"})
        return self.call("render_shader", {"glsl_code": BASE_CODE})

    def repair_then_finish(self, request: dict[str, object]) -> dict[str, object]:
        if not tool_results(request):
            return self.call("render_shader", {"glsl_code": "void mainImage(out vec4 c, in vec2 p) { c = vec4(unknown_symbol); }"})
        return self.render_then_finish(request)

    def respond(self, _client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
        self.assertEqual(str(request.url), "https://shader-deep.invalid/v1/chat/completions")
        payload = json.loads(request.content)
        self.requests.append(payload)
        message = self.response(payload)
        finish_reason = message.pop("_fixture_finish_reason", "tool_calls" if "tool_calls" in message else "stop")
        if payload.get("stream"):
            delta = {**message}
            if "tool_calls" in delta:
                delta["tool_calls"] = [{**call, "index": index} for index, call in enumerate(delta["tool_calls"])]
            base = {"id": "fixture-response", "object": "chat.completion.chunk", "created": 0, "model": "fixture-model"}
            frames = [
                {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
                {**base, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
            return httpx2.Response(200, request=request, content=body.encode(), headers={"content-type": "text/event-stream"})
        return httpx2.Response(
            200,
            request=request,
            json={
                "id": "fixture-response",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
