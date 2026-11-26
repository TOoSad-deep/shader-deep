"""用真实 SDK 与本地 HTTP 回执检验全局 token 账本."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from unittest.mock import patch

import httpx2
from openai import APIConnectionError

from shader_deep.infrastructure.llm.budget import ModelBudgetExhaustedError, RequestBudget, TokenBalance, meter_requests
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.infrastructure.test_config import OPENROUTER

_USAGE = {"prompt_tokens": 450, "completion_tokens": 50, "total_tokens": 500}


def _completion_reply(request: httpx2.Request, *, usage: bool = True) -> httpx2.Response:
    choice = {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    payload: dict[str, object] = {"id": "local", "model": "fixture", "created": 0, "choices": [choice]}
    if usage:
        payload["usage"] = _USAGE
    return httpx2.Response(200, json=payload, request=request)


def _completed_stream_reply(request: httpx2.Request) -> httpx2.Response:
    choice = {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    base = {"id": "local", "model": "fixture", "created": 0, "object": "chat.completion.chunk"}
    chunks = [{**base, "choices": [choice]}, {**base, "choices": [], "usage": _USAGE}]
    frames = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return httpx2.Response(200, content=(frames + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"}, request=request)


def _stream_reply(_client: object, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
    name = json.loads(request.content)["messages"][-1]["content"]
    complete = {"prompt_tokens": 19, "completion_tokens": 67, "total_tokens": 86}
    partial = {"prompt_tokens": 19, "completion_tokens": 1, "total_tokens": 20}
    delta = {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}
    terminal = {"index": 0, "delta": {}, "finish_reason": "stop"}
    chunks = [{"choices": [delta], "usage": partial}]
    if name == "completed":
        chunks.extend([{"choices": [terminal]}, {"choices": [terminal], "usage": complete}, {"choices": [terminal], "usage": complete}])
    payload = "".join("data: " + json.dumps({"id": name, "model": "fixture", "created": 1, **chunk}) + "\n\n" for chunk in chunks)
    return httpx2.Response(200, content=(payload + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"}, request=request)


class WorkflowBudgetTests(unittest.TestCase):
    def test_each_transport_reserves_before_sending_and_settles_confirmed_usage(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                self._check_requests(stream=stream)

    def _check_requests(self, *, stream: bool) -> None:
        sent = []
        meter = RequestBudget(TokenBalance(24, 50, 0, 0, 0, 0), 32, lambda _: None)
        usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
        common = {"id": "local", "model": "fixture", "created": 1}

        def reply(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            sent.append(json.loads(request.content))
            if not stream:
                choice = {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                return httpx2.Response(200, json={**common, "object": "chat.completion", "choices": [choice], "usage": usage}, request=request)
            choice = {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            chunks = [{"choices": [choice]}, {"choices": [], "usage": usage}]
            frames = "".join("data: " + json.dumps({**common, **chunk, "object": "chat.completion.chunk"}) + "\n\n" for chunk in chunks)
            return httpx2.Response(
                200, content=(frames + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"}, request=request
            )

        with meter_requests(meter), patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.Client.send", autospec=True, side_effect=reply):
            model = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=stream))
            for _ in range(2):
                model.invoke("local")
            with self.assertRaises(ModelBudgetExhaustedError):
                model.invoke("blocked")
        self.assertEqual([request["max_tokens"] for request in sent], [24, 19])
        self.assertEqual(meter.balance, TokenBalance(14, 16, 24, 10, 2, 0))

    def test_parallel_workers_share_the_last_output_allocation_without_usage(self) -> None:
        meter = RequestBudget(TokenBalance(5, None, 0, 0, 0, 0), None, lambda _: None)

        def reply(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            return _completion_reply(request, usage=False)

        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=reply) as wire,
        ):
            model = build_model()
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(copy_context().run, model.invoke, "local") for _ in range(2)]
                errors = [job.exception() for job in jobs]
        self.assertEqual(sum(isinstance(error, ModelBudgetExhaustedError) for error in errors), 1)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(meter.balance, TokenBalance(0, None, 0, 0, 1, 1))

    def test_parallel_request_waits_for_a_confirmed_refund(self) -> None:
        meter = RequestBudget(TokenBalance(1000, 10_000, 0, 0, 0, 0), 8000, lambda _: None)
        first_at_transport, release_first = Event(), Event()

        def reply(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            if json.loads(request.content)["messages"][-1]["content"] == "first":
                first_at_transport.set()
                self.assertTrue(release_first.wait(timeout=5))
            return _completion_reply(request)

        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=reply),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            model = build_model()
            first = pool.submit(copy_context().run, model.invoke, "first")
            try:
                self.assertTrue(first_at_transport.wait(timeout=5))
                second = pool.submit(copy_context().run, model.invoke, "second")
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=5).content, "ok")
            self.assertEqual(second.result(timeout=5).content, "ok")
        self.assertIsNone(meter.exhausted)
        self.assertEqual(meter.balance, TokenBalance(900, 9000, 900, 100, 2, 0))

    def test_async_requests_wait_without_blocking_the_refund(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                self._check_async_wait(stream=stream)

    def _check_async_wait(self, *, stream: bool) -> None:
        meter = RequestBudget(TokenBalance(1000, 10_000, 0, 0, 0, 0), 8000, lambda _: None)

        async def request_pair() -> None:
            first_at_transport, release_first = asyncio.Event(), asyncio.Event()

            async def reply(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
                if json.loads(request.content)["messages"][-1]["content"] == "first":
                    first_at_transport.set()
                    await release_first.wait()
                return _completed_stream_reply(request) if stream else _completion_reply(request)

            with patch("httpx2.AsyncClient.send", autospec=True, side_effect=reply):
                model = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=stream))
                async with asyncio.TaskGroup() as tasks:
                    first = tasks.create_task(model.ainvoke("first"))
                    await first_at_transport.wait()
                    second = tasks.create_task(model.ainvoke("second"))
                    done, _ = await asyncio.wait({second}, timeout=0.1)
                    self.assertFalse(done)
                    release_first.set()
                self.assertEqual(first.result().content, "ok")
                self.assertEqual(second.result().content, "ok")

        with meter_requests(meter), patch.dict(os.environ, OPENROUTER, clear=True):
            asyncio.run(asyncio.wait_for(request_pair(), timeout=5))
        self.assertIsNone(meter.exhausted)
        self.assertEqual(meter.balance, TokenBalance(900, 9000, 900, 100, 2, 0))

    def test_provider_overrun_still_permanently_stops_the_budget(self) -> None:
        meter = RequestBudget(TokenBalance(1000, 20_000, 0, 0, 0, 0), 8000, lambda _: None)
        ticket = meter.reserve(100)
        with self.assertRaisesRegex(ModelBudgetExhaustedError, "exceeded the declared request limit"):
            meter.settle(ticket, 100, 101, 201)
        with self.assertRaisesRegex(ModelBudgetExhaustedError, "exceeded the declared request limit"):
            meter.reserve(100)
        self.assertEqual(meter.balance, TokenBalance(899, 19_799, 100, 101, 1, 0))

    def test_missing_usage_or_transport_error_ends_the_in_flight_reservation(self) -> None:
        for failed in (False, True):
            with self.subTest(transport_failure=failed):
                self._check_pending_cleanup(failed=failed)

    def _check_pending_cleanup(self, *, failed: bool) -> None:
        meter = RequestBudget(TokenBalance(1000, 10_000, 0, 0, 0, 0), 8000, lambda _: None)

        def reply(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            if failed:
                msg = "fixture transport failure"
                raise httpx2.ConnectError(msg, request=request)
            return _completion_reply(request, usage=False)

        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=reply) as wire,
        ):
            model = build_model()
            if failed:
                with self.assertRaises(APIConnectionError):
                    model.invoke("first")
            else:
                model.invoke("first")
            with self.assertRaisesRegex(ModelBudgetExhaustedError, "Global token budget cannot fund"):
                model.invoke("second")
        self.assertIsNotNone(meter.exhausted)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(meter.balance, TokenBalance(0, 2000, 0, 0, 1, 1))

    def test_interleaved_streams_settle_only_the_completed_request_once(self) -> None:
        meter = RequestBudget(TokenBalance(180, None, 0, 0, 0, 0), None, lambda _: None)
        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=_stream_reply),
        ):
            model = configure_analysis_model(build_model(), AnalysisOptions(max_output_tokens=120))
            completed, interrupted = model.stream("completed"), model.stream("interrupted")
            next(completed)
            next(interrupted)
            list(completed)
            list(interrupted)
        self.assertEqual(meter.balance, TokenBalance(53, None, 19, 67, 2, 1))

    def test_async_stream_settles_usage_on_a_repeated_finish_choice(self) -> None:
        meter = RequestBudget(TokenBalance(180, None, 0, 0, 0, 0), None, lambda _: None)

        async def consume() -> None:
            model = configure_analysis_model(build_model(), AnalysisOptions(max_output_tokens=120))
            completed, interrupted = model.astream("completed"), model.astream("interrupted")
            await anext(completed)
            await anext(interrupted)
            async for _ in completed:
                pass
            async for _ in interrupted:
                pass

        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.AsyncClient.send", autospec=True, side_effect=_stream_reply),
        ):
            asyncio.run(consume())
        self.assertEqual(meter.balance, TokenBalance(53, None, 19, 67, 2, 1))

    def test_abandoned_stream_keeps_its_reservation(self) -> None:
        meter = RequestBudget(TokenBalance(120, None, 0, 0, 0, 0), None, lambda _: None)
        with (
            meter_requests(meter),
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("httpx2.Client.send", autospec=True, side_effect=_stream_reply),
        ):
            model = configure_analysis_model(build_model(), AnalysisOptions(max_output_tokens=120))
            stream = model.stream("completed")
            next(stream)
            stream.close()
        self.assertEqual(meter.balance, TokenBalance(0, None, 0, 0, 1, 1))
