"""用真实 SDK 解析模拟 SSE, 验证完整性检查发生在工具执行之前."""

import json
import os
from collections.abc import AsyncIterator, Iterator
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import httpx2
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from shader_deep.experiments._parameter_model import ParameterOutputError, configure_parameter_model
from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_run import run_parameter_task
from tests.unit_tests.agents.test_parameter_execution import preview_fixture

COMPLETE_ARGS = '{"parameters":{"RED":0.5}}'
PARTIAL_ARGS = '{"parameters":{"RED":0.5'


def response(request: httpx2.Request, *, tool: bool, finishes: tuple[str, ...], arguments: str = COMPLETE_ARGS) -> httpx2.Response:
    base = {"id": "offline-stream", "created": 0, "model": "offline-model", "object": "chat.completion.chunk"}
    delta = {"role": "assistant", "content": "" if tool else "done"}
    if tool:
        delta["tool_calls"] = [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "set_parameters", "arguments": arguments}}]
    frames = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}]
    frames.extend({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]} for finish in finishes)
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    if finishes:
        body += "data: [DONE]\n\n"
    return httpx2.Response(200, request=request, content=body.encode(), headers={"content-type": "text/event-stream"})


class TaggedModel(ChatOpenAI):
    """验证 guard 保留调用方子类的载荷覆盖方法."""

    def _get_request_payload(self, *args: object, **kwargs: object) -> dict:
        payload = super()._get_request_payload(*args, **kwargs)
        payload["user"] = "subclass-retained"
        return payload


class Recorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.finishes = []

    def on_llm_end(self, result: object, **kwargs: object) -> None:
        del kwargs
        self.finishes.extend(g.message.response_metadata.get("finish_reason") for batch in result.generations for g in batch)


class ParameterStreamTests(TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_normal_and_duplicate_terminal_frames_execute_one_tool_and_preserve_subclass(self) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                preview = preview_fixture()
                model = TaggedModel(api_key="offline", model="offline-model", streaming=True)
                agent = build_parameter_agent(preview=preview, model=model)
                recorder = Recorder()
                payloads = []

                def send(
                    _client: httpx2.Client, request: httpx2.Request, *, payloads: list = payloads, duplicate: bool = duplicate, **_kwargs: object
                ) -> httpx2.Response:
                    payloads.append(json.loads(request.content))
                    reason = "tool_calls" if len(payloads) == 1 else "stop"
                    return response(request, tool=len(payloads) == 1, finishes=(reason,) * (2 if duplicate else 1))

                with patch("httpx2.Client.send", autospec=True, side_effect=send):
                    result = run_parameter_task(agent, HumanMessage(content="adjust RED"), preview, config={"callbacks": [recorder]})
                self.assertEqual(result.model_calls, 2)
                self.assertEqual(recorder.finishes, ["tool_calls", "stop"])
                self.assertTrue(all(p["user"] == "subclass-retained" and p["stream"] for p in payloads))
                preview.set_parameters.assert_called_once_with({"RED": 0.5})

    def test_abnormal_terminal_or_partial_arguments_never_execute_tools(self) -> None:
        for finishes, arguments in (
            (("length",), PARTIAL_ARGS),
            (("length", "length"), PARTIAL_ARGS),
            ((), PARTIAL_ARGS),
            (("tool_calls", "stop"), COMPLETE_ARGS),
            (("length", "tool_calls"), COMPLETE_ARGS),
            (("tool_calls",), PARTIAL_ARGS),
        ):
            with self.subTest(finishes=finishes, arguments=arguments):
                preview = preview_fixture()
                model = ChatOpenAI(api_key="offline", model="offline-model", streaming=True)
                agent = build_parameter_agent(preview=preview, model=model)

                def send(
                    _client: httpx2.Client,
                    request: httpx2.Request,
                    *,
                    finishes: tuple[str, ...] = finishes,
                    arguments: str = arguments,
                    **_kwargs: object,
                ) -> httpx2.Response:
                    return response(request, tool=True, finishes=finishes, arguments=arguments)

                with patch("httpx2.Client.send", autospec=True, side_effect=send) as transport, self.assertRaises(ParameterOutputError):
                    run_parameter_task(agent, HumanMessage(content="adjust RED"), preview)
                self.assertEqual(transport.call_count, 1)
                preview.set_parameters.assert_not_called()
                preview.capture.assert_not_called()

    def test_stream_close_failure_preserves_primary_output_error(self) -> None:
        closed = []

        class Body(httpx2.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                yield data

            def close(self) -> None:
                closed.append(True)
                msg = "stream close failed"
                raise RuntimeError(msg)

        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        data = response(request, tool=True, finishes=("tool_calls", "stop")).content

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return httpx2.Response(200, request=request, stream=Body(), headers={"content-type": "text/event-stream"})

        with patch("httpx2.Client.send", autospec=True, side_effect=send):
            agent = build_parameter_agent(model=ChatOpenAI(api_key="offline", model="offline-model", streaming=True))
            with self.assertRaises(ParameterOutputError) as caught:
                agent.invoke({"messages": [HumanMessage(content="hello")]})
            self.assertEqual(closed, [True])
            self.assertTrue(any("stream close failed" in note for note in caught.exception.__notes__))


class AsyncParameterStreamTests(IsolatedAsyncioTestCase):
    async def test_conflict_and_later_chunks_are_drained_without_being_exposed(self) -> None:
        closed = []
        delivered = []

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield data

            async def aclose(self) -> None:
                closed.append(True)

        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        late = {
            "id": "offline-stream",
            "created": 0,
            "model": "offline-model",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "must not escape"}, "finish_reason": None}],
        }
        suffix = ("data: " + json.dumps(late) + "\n\ndata: [DONE]\n\n").encode()
        data = response(request, tool=True, finishes=("tool_calls", "stop")).content.replace(b"data: [DONE]\n\n", suffix)

        async def send(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return httpx2.Response(200, request=request, stream=Body(), headers={"content-type": "text/event-stream"})

        with patch.dict(os.environ, {}, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
            model = configure_parameter_model(ChatOpenAI(api_key="offline", model="offline-model", streaming=True))
            with self.assertRaises(ParameterOutputError):
                async for chunk in model.astream([HumanMessage(content="hello")]):
                    self.assertNotIn("must not escape", str(chunk.content))
                    delivered.append(chunk)
            self.assertEqual(len(delivered), 2)
            self.assertEqual([c.response_metadata.get("finish_reason") for c in delivered], [None, "tool_calls"])
            self.assertEqual(closed, [True])
            preview = preview_fixture()
            with self.assertRaises(ParameterOutputError):
                await build_parameter_agent(preview=preview, model=model).ainvoke({"messages": [HumanMessage(content="hello")]})
            preview.set_parameters.assert_not_called()
            self.assertEqual(closed, [True, True])

    async def test_conflicting_marker_closes_async_response_before_returning_error(self) -> None:
        closed = []

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield data

            async def aclose(self) -> None:
                closed.append(True)

        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        data = response(request, tool=True, finishes=("tool_calls", "stop")).content

        async def send(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return httpx2.Response(200, request=request, stream=Body(), headers={"content-type": "text/event-stream"})

        with patch.dict(os.environ, {}, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
            agent = build_parameter_agent(model=ChatOpenAI(api_key="offline", model="offline-model", streaming=True))
            with self.assertRaises(ParameterOutputError):
                await agent.ainvoke({"messages": [HumanMessage(content="hello")]})
            self.assertEqual(closed, [True])

    async def test_async_stream_close_failure_preserves_primary_output_error(self) -> None:
        closed = []

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield data

            async def aclose(self) -> None:
                closed.append(True)
                msg = "async stream close failed"
                raise RuntimeError(msg)

        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        data = response(request, tool=True, finishes=("tool_calls", "stop")).content

        async def send(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return httpx2.Response(200, request=request, stream=Body(), headers={"content-type": "text/event-stream"})

        with patch.dict(os.environ, {}, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
            agent = build_parameter_agent(model=ChatOpenAI(api_key="offline", model="offline-model", streaming=True))
            with self.assertRaises(ParameterOutputError) as caught:
                await agent.ainvoke({"messages": [HumanMessage(content="hello")]})
            self.assertEqual(closed, [True])
            self.assertTrue(any("async stream close failed" in note for note in caught.exception.__notes__))

    async def test_async_normal_and_duplicate_terminal_frames_execute_one_tool(self) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), patch.dict(os.environ, {}, clear=True):
                preview = preview_fixture()
                model = TaggedModel(api_key="offline", model="offline-model", streaming=True)
                agent = build_parameter_agent(preview=preview, model=model)
                recorder = Recorder()
                payloads = []

                async def send(
                    _client: httpx2.AsyncClient, request: httpx2.Request, *, payloads: list = payloads, duplicate: bool = duplicate, **_kwargs: object
                ) -> httpx2.Response:
                    payloads.append(json.loads(request.content))
                    reason = "tool_calls" if len(payloads) == 1 else "stop"
                    return response(request, tool=len(payloads) == 1, finishes=(reason,) * (2 if duplicate else 1))

                with patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
                    result = await agent.ainvoke({"messages": [HumanMessage(content="adjust RED")]}, config={"callbacks": [recorder]})
                self.assertEqual(result["messages"][-1].content, "done")
                self.assertEqual(recorder.finishes, ["tool_calls", "stop"])
                self.assertTrue(all(p["user"] == "subclass-retained" and p["stream"] for p in payloads))
                preview.set_parameters.assert_called_once_with({"RED": 0.5})

    async def test_async_abnormal_terminal_or_partial_arguments_never_execute_tools(self) -> None:
        for finishes, arguments in (
            (("length",), PARTIAL_ARGS),
            (("length", "length"), PARTIAL_ARGS),
            ((), PARTIAL_ARGS),
            (("tool_calls", "stop"), COMPLETE_ARGS),
            (("length", "tool_calls"), COMPLETE_ARGS),
            (("tool_calls",), PARTIAL_ARGS),
        ):
            with self.subTest(finishes=finishes, arguments=arguments), patch.dict(os.environ, {}, clear=True):
                preview = preview_fixture()
                model = ChatOpenAI(api_key="offline", model="offline-model", streaming=True)
                agent = build_parameter_agent(preview=preview, model=model)

                async def send(
                    _client: httpx2.AsyncClient,
                    request: httpx2.Request,
                    *,
                    finishes: tuple[str, ...] = finishes,
                    arguments: str = arguments,
                    **_kwargs: object,
                ) -> httpx2.Response:
                    return response(request, tool=True, finishes=finishes, arguments=arguments)

                with patch("httpx2.AsyncClient.send", autospec=True, side_effect=send) as transport, self.assertRaises(ParameterOutputError):
                    await agent.ainvoke({"messages": [HumanMessage(content="adjust RED")]})
                self.assertEqual(transport.call_count, 1)
                preview.set_parameters.assert_not_called()
