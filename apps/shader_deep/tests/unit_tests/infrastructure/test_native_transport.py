"""原生框架执行前检查真实 SDK 原文与结束标记, 避免补全参数被执行."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

import httpx2
from langchain_core.exceptions import ModelAPIError
from langchain_core.messages import HumanMessage

from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage


def _fixture(arguments: str, *, streaming: bool, finish: str | None = "tool_calls") -> AIMessage:
    """复用真实 HTTP 编解码夹具, 只改变本测试关心的原文与结束原因."""
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-probe", "type": "function", "function": {"name": "probe", "arguments": arguments}}],
    }

    def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
        response = _response(request, message, streaming=streaming)
        body = response.content.decode()
        if streaming:
            body = body.replace('"finish_reason": "tool_calls"', '"finish_reason": ' + json.dumps(finish))
        else:
            payload = json.loads(body)
            payload["choices"][0]["finish_reason"] = finish
            body = json.dumps(payload)
        return httpx2.Response(200, request=request, content=body.encode(), headers=response.headers)

    with patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.Client.send", autospec=True, side_effect=respond):
        model = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=streaming))
        return model.invoke([HumanMessage(content="读取数值")])


class NativeTransportTests(TestCase):
    def test_auto_completed_or_ambiguous_json_is_never_accepted(self) -> None:
        for streaming, arguments in ((True, '{"value": 1'), (False, '{"value":1,"value":2}')):
            with self.subTest(streaming=streaming, arguments=arguments):
                response = _fixture(arguments, streaming=streaming)
                # SDK 确实已产生可执行调用, 拒绝必须发生在工具运行之前.
                self.assertTrue(response.tool_calls)
                with self.assertRaises(ValueError):
                    validate_analysis_message(response)

    def test_missing_finish_is_retryable_and_length_is_explicit_failure(self) -> None:
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                response = _fixture('{"value":1}', streaming=streaming, finish=None)
                with self.assertRaises(ModelAPIError) as raised:
                    validate_analysis_message(response)
                self.assertTrue(_retryable(raised.exception))
                response = _fixture('{"value":1}', streaming=streaming, finish="length")
                with self.assertRaisesRegex(ValueError, "output limit; no tools executed"):
                    validate_analysis_message(response)

    def test_valid_original_parameters_replace_client_interpretation(self) -> None:
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                response = _fixture('{"value":1}', streaming=streaming)
                response.tool_calls[0]["args"]["value"] = 9
                accepted = validate_analysis_message(response)
                self.assertEqual(accepted.tool_calls[0]["args"], {"value": 1})
