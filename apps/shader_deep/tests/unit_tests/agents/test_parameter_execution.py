"""通过真实 SDK 编解码和原生 Agent 图验证故障边界, 不访问网络."""

import json
import os
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

import httpx2
from langchain_core.exceptions import ModelTimeoutError
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langsmith import Client, tracing_context

from shader_deep.experiments._parameter_model import ParameterOutputError
from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_run import ParameterBudgetError, run_parameter_task
from shader_deep.rendering import PreviewSnapshot, RenderError, ShaderPreview


def reply(request: httpx2.Request, *, tool: str | None = None, finish: str = "stop") -> httpx2.Response:
    message = {"role": "assistant", "content": "done"}
    if tool:
        arguments = {"parameters": {"RED": 0.5}} if tool == "set_parameters" else {}
        message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]
    return httpx2.Response(
        200,
        request=request,
        json={
            "id": "offline",
            "model": "offline-model",
            "created": 0,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def preview_fixture() -> Mock:
    preview = Mock(spec=ShaderPreview)
    preview.parameter_specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
    preview.set_parameters.return_value = {"RED": 0.5}
    preview.capture.return_value = PreviewSnapshot(png=b"png", parameters={"RED": 0.5}, revision=1, time=0.0, width=1, height=1)
    return preview


class ParameterExecutionTests(TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.preview = preview_fixture()
        self.task = HumanMessage(content="adjust RED")
        self.model = ChatOpenAI(api_key="offline-test", model="offline-model", max_retries=5)
        self.agent = build_parameter_agent(preview=self.preview, model=self.model)

    def test_retry_counts_real_attempts_without_replaying_successful_tool(self) -> None:
        payloads = []
        failed_attempt = 2

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                return reply(request, tool="set_parameters", finish="tool_calls")
            if len(payloads) == failed_attempt:
                msg = "TLS handshake"
                raise httpx2.ConnectTimeout(msg, request=request)
            return reply(request)

        with patch("httpx2.Client.send", autospec=True, side_effect=send), patch("langchain.agents.middleware.model_retry.time.sleep"):
            result = run_parameter_task(self.agent, self.task, self.preview)
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(payloads[1], payloads[2])
        self.assertEqual(payloads[0]["max_completion_tokens"], 8192)
        self.preview.set_parameters.assert_called_once_with({"RED": 0.5})
        self.assertIs(result.snapshot, self.preview.capture.return_value)
        self.assertEqual(self.model.max_retries, 5)
        self.assertIsNone(self.model.max_tokens)

    def test_retry_is_bounded_and_total_budget_includes_failed_attempt(self) -> None:
        for limit, expected, exception in ((20, 2, ModelTimeoutError), (1, 1, ParameterBudgetError)):
            with self.subTest(limit=limit):

                def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
                    msg = "TLS handshake"
                    raise httpx2.ConnectTimeout(msg, request=request)

                with (
                    patch("httpx2.Client.send", autospec=True, side_effect=send) as transport,
                    patch("langchain.agents.middleware.model_retry.time.sleep"),
                    self.assertRaises(exception),
                ):
                    run_parameter_task(self.agent, self.task, self.preview, max_model_calls=limit)
                self.assertEqual(transport.call_count, expected)
        self.preview.capture.assert_not_called()

    def test_read_timeout_is_not_retried_or_overwritten_by_capture(self) -> None:
        self.preview.capture.side_effect = RenderError("browser closed", stage="browser")
        with patch("httpx2.Client.send", side_effect=httpx2.ReadTimeout("read timeout")) as transport, self.assertRaises(ModelTimeoutError):
            run_parameter_task(self.agent, self.task, self.preview)
        self.assertEqual(transport.call_count, 1)
        self.preview.capture.assert_not_called()

    def test_truncated_tool_arguments_never_execute(self) -> None:
        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return reply(request, tool="set_parameters", finish="length")

        with patch("httpx2.Client.send", autospec=True, side_effect=send) as transport, self.assertRaises(ParameterOutputError):
            run_parameter_task(self.agent, self.task, self.preview)
        self.assertEqual(transport.call_count, 1)
        self.preview.set_parameters.assert_not_called()
        self.preview.capture.assert_not_called()

    def test_fatal_browser_error_ends_graph_without_another_model_call(self) -> None:
        error = RenderError("Target page has been closed", stage="browser")
        self.preview.capture.side_effect = error

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return reply(request, tool="capture_preview", finish="tool_calls")

        with patch("httpx2.Client.send", autospec=True, side_effect=send) as transport, self.assertRaises(RenderError) as caught:
            run_parameter_task(self.agent, self.task, self.preview)
        self.assertIs(caught.exception, error)
        self.assertEqual(transport.call_count, 1)
        self.preview.capture.assert_called_once()

    def test_final_capture_failure_is_not_success(self) -> None:
        self.preview.capture.side_effect = RenderError("closed after graph", stage="browser")

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return reply(request)

        with patch("httpx2.Client.send", autospec=True, side_effect=send), self.assertRaisesRegex(RenderError, "closed after graph"):
            run_parameter_task(self.agent, self.task, self.preview)

    def test_trace_root_includes_final_capture_failure(self) -> None:
        client = Mock(spec=Client)
        self.preview.capture.side_effect = RenderError("closed after graph", stage="browser")

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return reply(request)

        with (
            tracing_context(enabled=True, client=client),
            patch("httpx2.Client.send", autospec=True, side_effect=send),
            self.assertRaises(RenderError),
        ):
            run_parameter_task(self.agent, self.task, self.preview)
        roots = [call.kwargs for call in client.create_run.call_args_list if call.kwargs.get("name") == "shader-deep.parameter-run"]
        self.assertEqual(len(roots), 1)
        root_id = roots[0]["id"]
        errors = [call.kwargs.get("error", "") for call in client.update_run.call_args_list if call.kwargs.get("run_id") == root_id]
        self.assertTrue(any("closed after graph" in error for error in errors))

    def test_explicit_output_cap_is_preserved_on_the_wire(self) -> None:
        model = ChatOpenAI(api_key="offline-test", model="offline-model", max_tokens=12000)
        agent = build_parameter_agent(preview=self.preview, model=model)
        caps = []

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            caps.append(json.loads(request.content)["max_completion_tokens"])
            return reply(request)

        with patch("httpx2.Client.send", autospec=True, side_effect=send):
            run_parameter_task(agent, self.task, self.preview)
        self.assertEqual(caps, [12000])


class AsyncParameterExecutionTests(IsolatedAsyncioTestCase):
    async def test_async_requests_retry_once_and_reject_truncation(self) -> None:
        preview = preview_fixture()
        model = ChatOpenAI(api_key="offline-test", model="offline-model", max_retries=5)
        agent = build_parameter_agent(preview=preview, model=model)
        requests = []

        async def send(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(request)
            if len(requests) == 1:
                msg = "TLS handshake"
                raise httpx2.ConnectTimeout(msg, request=request)
            return reply(request, tool="set_parameters", finish="length")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("httpx2.AsyncClient.send", autospec=True, side_effect=send),
            patch("langchain.agents.middleware.model_retry.asyncio.sleep"),
            self.assertRaises(ParameterOutputError),
        ):
            await agent.ainvoke({"messages": [HumanMessage(content="adjust RED")]})
        self.assertEqual(len(requests), 2)
        preview.set_parameters.assert_not_called()
