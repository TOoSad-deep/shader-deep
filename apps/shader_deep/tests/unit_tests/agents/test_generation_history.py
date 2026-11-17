"""验证生成历史投影后的真实请求格式、正文回读和原执行消息不变."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, TypedDict
from unittest import TestCase
from unittest.mock import patch

import httpx2
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.generation.history import MessageContent, RenderHistoryReference, project_history

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

BASE_CODE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(0.1); }"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1kAAAAASUVORK5CYII=")


class WireCall(TypedDict):
    id: str
    function: dict[str, str]


class WireMessage(TypedDict, total=False):
    role: str
    content: MessageContent | None
    tool_calls: list[WireCall]
    tool_call_id: str


class WireRequest(TypedDict):
    messages: list[WireMessage]


class GenerationHistoryTests(TestCase):
    def setUp(self) -> None:
        self.requests: list[WireRequest] = []
        tracing = patch.dict(os.environ, {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
        tracing.start()
        self.addCleanup(tracing.stop)
        transport = patch("httpx2.Client.send", autospec=True, side_effect=self.respond)
        transport.start()
        self.addCleanup(transport.stop)
        self.model = ChatOpenAI(model="fixture-model", api_key="fixture-key", base_url="https://shader-deep.invalid/v1", use_responses_api=False)
        self.reference = RenderHistoryReference(candidate_id="c001", code_material_id="m0001", result_material_id="m0002", status="render_error")
        self.render = AIMessage(
            content="观察结论仍保留",
            tool_calls=[{"name": "render_shader", "args": {"glsl_code": BASE_CODE}, "id": "render-1", "type": "tool_call"}],
        )
        self.result = ToolMessage(
            content=json.dumps({"status": "render_error", "candidate_id": "c001", "error": "unique driver failure"}),
            tool_call_id="render-1",
            name="render_shader",
        )
        self.messages: list[BaseMessage] = [HumanMessage(content="生成图像"), self.render, self.result]

    def respond(self, _client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
        self.assertEqual(str(request.url), "https://shader-deep.invalid/v1/chat/completions")
        self.requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            request=request,
            json={
                "id": "fixture-response",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "已读取材料"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def project(self, read_contents: dict[str, MessageContent] | None = None) -> list[BaseMessage]:
        return project_history(self.messages, rendered_calls={"render-1": self.reference}, read_contents=read_contents)

    def send(self, messages: list[BaseMessage]) -> WireRequest:
        self.model.invoke(messages)
        return self.requests[-1]

    def add_read(self) -> None:
        self.messages.extend(
            [
                AIMessage(content="", tool_calls=[{"name": "read_materials", "args": {"items": [{"id": "m0001"}]}, "id": "read-1"}]),
                ToolMessage(content='{"materials": [{"id": "m0001", "status": "read"}]}', tool_call_id="read-1", name="read_materials"),
            ]
        )

    def test_registered_render_payloads_exit_the_whole_serialized_request(self) -> None:
        before = [message.model_dump() for message in self.messages]
        request = self.send(self.project())
        wire = json.dumps(request)
        self.assertNotIn(BASE_CODE, wire)
        self.assertNotIn("unique driver failure", wire)
        self.assertIn("m0001", wire)
        self.assertIn("m0002", wire)
        self.assertIn("观察结论仍保留", json.dumps(request, ensure_ascii=False))
        self.assertEqual([message.model_dump() for message in self.messages], before)
        self.assertEqual(request["messages"][1]["tool_calls"][0]["id"], request["messages"][2]["tool_call_id"])

    def test_default_and_recalled_code_do_not_reappear_in_historical_arguments(self) -> None:
        current: list[BaseMessage] = [HumanMessage(content=BASE_CODE), *self.project()]
        self.assertEqual(json.dumps(self.send(current)).count(BASE_CODE), 1)
        self.add_read()
        first = self.send(self.project({"read-1": BASE_CODE}))
        self.assertEqual(json.dumps(first).count(BASE_CODE), 1)
        self.assertNotIn(BASE_CODE, json.dumps(first["messages"][1]["tool_calls"]))
        self.assertNotIn(BASE_CODE, json.dumps(self.send(self.project())))
        self.assertEqual(json.dumps(self.send(self.project({"read-1": BASE_CODE}))).count(BASE_CODE), 1)

    def test_actual_image_blocks_reach_tool_response_once_then_leave(self) -> None:
        self.add_read()
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        content: MessageContent = [{"type": "text", "text": "m0003: 完整预览"}, {"type": "image_url", "image_url": {"url": url}}]
        request = self.send(self.project({"read-1": content}))
        self.assertEqual(request["messages"][-1]["role"], "tool")
        self.assertEqual(request["messages"][-1]["tool_call_id"], "read-1")
        self.assertEqual(request["messages"][-1]["content"], content)
        self.assertNotIn(url, json.dumps(self.send(self.project())))

    def test_unregistered_and_unexecuted_calls_keep_their_original_payload(self) -> None:
        self.assertEqual(project_history(self.messages, rendered_calls={})[1:], self.messages[1:])
        no_response = project_history(self.messages[:-1], rendered_calls={"render-1": self.reference})
        self.assertEqual(no_response[1].model_dump(), self.render.model_dump())

    def test_raw_argument_copies_are_projected_without_touching_reasoning(self) -> None:
        reasoning = [{"type": "reasoning.encrypted", "data": "opaque-signature", "id": "r1"}]
        self.render.additional_kwargs = {
            "tool_calls": [
                {"id": "render-1", "type": "function", "function": {"name": "render_shader", "arguments": json.dumps({"glsl_code": BASE_CODE})}}
            ],
            "analysis_raw_tool_calls": [{"id": "render-1", "name": "render_shader", "args": json.dumps({"glsl_code": BASE_CODE})}],
            "reasoning_details": reasoning,
        }
        before = self.render.model_dump()
        projected = self.project()[1]
        self.assertNotIn(BASE_CODE, json.dumps(projected.additional_kwargs))
        self.assertEqual(projected.additional_kwargs["reasoning_details"], reasoning)
        self.assertEqual(self.render.model_dump(), before)

    def test_recalled_error_returns_once_from_the_result_material(self) -> None:
        self.add_read()
        result = json.dumps({"candidate_id": "c001", "error": "unique driver failure"})
        self.assertEqual(json.dumps(self.send(self.project({"read-1": result}))).count("unique driver failure"), 1)
        self.assertNotIn("unique driver failure", json.dumps(self.send(self.project())))

    def test_transport_retry_uses_identical_projected_materials(self) -> None:
        self.add_read()
        attempts: list[dict[str, object]] = []

        def respond(client: httpx2.Client, request: httpx2.Request, **kwargs: object) -> httpx2.Response:
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                return httpx2.Response(503, request=request, headers={"retry-after-ms": "1"}, json={"error": {"message": "temporary failure"}})
            return self.respond(client, request, **kwargs)

        with patch("httpx2.Client.send", new=respond):
            self.send(self.project({"read-1": BASE_CODE}))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["messages"], attempts[1]["messages"])
        self.assertEqual(json.dumps(attempts[0]).count(BASE_CODE), 1)

    def test_projected_payload_is_not_aliased_to_caller_content(self) -> None:
        self.add_read()
        block: dict[str, object] = {"type": "text", "text": BASE_CODE}
        content: MessageContent = [block]
        projected = self.project({"read-1": content})
        block["text"] = "modified"
        self.assertIn(BASE_CODE, json.dumps(projected[-1].content))
