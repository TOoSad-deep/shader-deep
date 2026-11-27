"""通过真实 Agent 图与模拟 HTTP 验证调参完成契约, 不访问外部模型."""

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import Self
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import httpx2
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_reference import freeze_parameter_reference
from shader_deep.experiments.parameter_run import ParameterBudgetError, ParameterProtocolError, run_parameter_task
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.rendering import PreviewSnapshot
from tests.unit_tests.agents.test_parameter_execution import preview_fixture


def png(color: str) -> bytes:
    with BytesIO() as buffer:
        with Image.new("RGB", (2, 2), color) as image:
            image.save(buffer, format="PNG")
        return buffer.getvalue()


def completion(request: httpx2.Request, names: tuple[str, ...]) -> httpx2.Response:
    calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"parameters": {"RED": 0.5}} if name == "set_parameters" else {})},
        }
        for index, name in enumerate(names)
    ]
    message = {"role": "assistant", "content": "完成", "tool_calls": calls}
    return httpx2.Response(
        200,
        request=request,
        json={
            "id": "offline",
            "model": "offline-model",
            "created": 0,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if names else "stop"}],
        },
    )


class ParameterPolicyTests(TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.preview = preview_fixture()
        self.preview.capture.return_value = PreviewSnapshot(png=png("black"), parameters={"RED": 0.5}, revision=1, time=0.0, width=2, height=2)
        self.model = ChatOpenAI(api_key="offline", model="offline-model", max_retries=0)
        self.agent = build_parameter_agent(preview=self.preview, model=self.model)
        self.task = HumanMessage(content="调整允许参数")
        self.requests = []

    def transport(self, responses: list[tuple[str, ...]]) -> object:
        replies = iter(responses)

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            self.requests.append(json.loads(request.content))
            return completion(request, next(replies))

        return patch("httpx2.Client.send", autospec=True, side_effect=send)

    def test_only_business_tools_are_sent_and_guessed_delegation_is_blocked(self) -> None:
        with self.transport([("task",)]), self.assertRaisesRegex(ParameterProtocolError, "allowlist"):
            run_parameter_task(self.agent, self.task, self.preview)
        names = {entry["function"]["name"] for entry in self.requests[0]["tools"]}
        self.assertEqual(names, {"set_parameters", "capture_preview"})
        self.assertEqual(len(self.requests), 1)
        self.preview.set_parameters.assert_not_called()
        self.preview.capture.assert_not_called()

    def test_batch_updates_and_capture_are_rejected_before_any_tool_executes(self) -> None:
        for tools in (("set_parameters", "capture_preview"), ("set_parameters", "set_parameters")):
            with self.subTest(tools=tools), self.transport([tools]), self.assertRaisesRegex(ParameterProtocolError, "only tool call"):
                run_parameter_task(self.agent, self.task, self.preview)
        self.preview.set_parameters.assert_not_called()
        self.preview.capture.assert_not_called()

    def test_update_then_text_does_not_create_a_verified_result(self) -> None:
        with self.transport([("set_parameters",), ()]), self.assertRaisesRegex(ParameterProtocolError, "must reach"):
            run_parameter_task(self.agent, self.task, self.preview)
        self.preview.set_parameters.assert_called_once()
        self.preview.capture.assert_not_called()

    def test_old_image_cannot_verify_a_later_update_or_another_update(self) -> None:
        for final in ((), ("set_parameters",)):
            self.preview.reset_mock()
            with (
                self.subTest(final=final),
                self.transport([("capture_preview",), ("set_parameters",), final]),
                self.assertRaises(ParameterProtocolError),
            ):
                run_parameter_task(self.agent, self.task, self.preview)
            self.preview.set_parameters.assert_called_once()
            self.preview.capture.assert_called_once()

    def test_three_calls_allow_one_update_capture_and_final_readback(self) -> None:
        with self.transport([("set_parameters",), ("capture_preview",), ()]):
            result = run_parameter_task(self.agent, self.task, self.preview, max_model_calls=3)
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(result.snapshot.revision, 1)
        self.assertEqual(dict(result.snapshot.parameters), {"RED": 0.5})
        self.assertTrue(any(message["role"] == "tool" and isinstance(message["content"], list) for message in self.requests[-1]["messages"]))
        # 运行私有 artifact 不进入实际 HTTP 载荷.
        self.assertNotIn("artifact", json.dumps(self.requests))

    def test_budget_hides_update_and_blocks_a_guessed_update(self) -> None:
        with self.transport([("set_parameters",)]), self.assertRaises(ParameterBudgetError) as caught:
            run_parameter_task(self.agent, self.task, self.preview, max_model_calls=1)
        self.assertNotIn("set_parameters", {entry["function"]["name"] for entry in self.requests[0]["tools"]})
        self.preview.set_parameters.assert_not_called()
        self.preview.capture.assert_not_called()
        self.assertEqual(caught.exception.progress.model_calls, 1)
        self.assertIsNone(caught.exception.progress.last_snapshot)

    def test_budget_exhaustion_keeps_current_parameters_and_presented_snapshot(self) -> None:
        with self.transport([("capture_preview",), ("set_parameters",)]), self.assertRaises(ParameterBudgetError) as caught:
            run_parameter_task(self.agent, self.task, self.preview, max_model_calls=2)
        progress = caught.exception.progress
        self.assertEqual(progress.model_calls, 2)
        self.assertEqual(dict(progress.parameters), {"RED": 0.5})
        self.assertEqual(progress.presented_revision, 1)
        self.assertIs(progress.last_snapshot, self.preview.capture.return_value)
        self.preview.set_parameters.assert_not_called()

    def test_denied_update_can_finish_using_existing_observation(self) -> None:
        with self.transport([("capture_preview",), ("set_parameters",), ()]):
            result = run_parameter_task(self.agent, self.task, self.preview, max_model_calls=3)
        update = next(message for message in result.messages if isinstance(message, ToolMessage) and message.name == "set_parameters")
        self.assertEqual(update.status, "error")
        self.preview.set_parameters.assert_not_called()

    def test_final_state_drift_is_detected_without_trusting_model_text(self) -> None:
        previous = self.preview.capture.return_value
        for field, value in (("revision", 2), ("png", png("red")), ("time", 1.0), ("parameters", {"RED": 0.7})):
            changed = {
                "png": previous.png,
                "parameters": previous.parameters,
                "revision": previous.revision,
                "time": previous.time,
                "width": 2,
                "height": 2,
            }
            changed[field] = value
            self.preview.capture.side_effect = [previous, PreviewSnapshot(**changed)]
            with self.subTest(field=field), self.transport([("capture_preview",), ()]), self.assertRaisesRegex(ParameterProtocolError, "changed"):
                run_parameter_task(self.agent, self.task, self.preview)

    def test_runner_rejects_another_preview_before_model_request(self) -> None:
        with self.transport([]), self.assertRaisesRegex(ValueError, "same instance"):
            run_parameter_task(self.agent, self.task, preview_fixture())
        self.assertEqual(self.requests, [])

    def test_reusing_agent_does_not_reuse_observation_from_previous_invocation(self) -> None:
        with self.transport([("capture_preview",), ()]):
            run_parameter_task(self.agent, self.task, self.preview)
        with self.transport([()]), self.assertRaisesRegex(ParameterProtocolError, "must reach"):
            run_parameter_task(self.agent, self.task, self.preview)

    def test_parallel_independent_runs_do_not_share_budget_or_preview_binding(self) -> None:
        barrier = Barrier(2)

        class ConcurrentModel(FakeMessagesListChatModel):
            def bind_tools(self, *_args: object, **_kwargs: object) -> Self:
                return self

            def _generate(self, *args: object, **kwargs: object) -> object:
                if self.i == 0:
                    barrier.wait(timeout=5)
                return super()._generate(*args, **kwargs)

        def execute(*, limited: bool) -> object:
            preview = preview_fixture()
            tool = "set_parameters" if limited else "capture_preview"
            args = {"parameters": {"RED": 0.5}} if limited else {}
            model = ConcurrentModel(
                responses=[AIMessage(content="", tool_calls=[{"id": "t", "name": tool, "args": args}]), AIMessage(content="done")]
            )
            agent = build_parameter_agent(preview=preview, model=model)
            return run_parameter_task(agent, self.task, preview, max_model_calls=1 if limited else 2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            limited = pool.submit(execute, limited=True)
            complete = pool.submit(execute, limited=False)
            with self.assertRaises(ParameterBudgetError) as caught:
                limited.result()
            self.assertEqual(caught.exception.progress.model_calls, 1)
            self.assertEqual(complete.result().model_calls, 2)

    def test_reference_changed_between_builders_is_rejected_before_request(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "reference.png"
            path.write_bytes(png("red"))
            task = build_reference_task(path)
            path.write_bytes(png("blue"))
            agent = build_parameter_agent(preview=self.preview, model=self.model, reference_path=path)
        with self.transport([]), self.assertRaisesRegex(ValueError, "frozen parameter reference"):
            run_parameter_task(agent, task, self.preview)
        self.assertEqual(self.requests, [])

    def test_shared_frozen_reference_survives_file_replacement(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "reference.png"
            path.write_bytes(png("red"))
            reference = freeze_parameter_reference(path)
            task = build_reference_task(reference)
            path.write_bytes(png("blue"))
            agent = build_parameter_agent(preview=self.preview, model=self.model, reference_path=reference)
        with self.transport([("compare_preview",), ()]):
            result = run_parameter_task(agent, task, self.preview)
        self.assertEqual(result.snapshot.png, png("black"))
        self.assertEqual(self.requests[0]["messages"][-1]["content"][1]["image_url"]["url"], reference.data_url)


class AsyncParameterPolicyTests(IsolatedAsyncioTestCase):
    async def test_async_delegation_batch_and_early_final_are_rejected(self) -> None:
        for sequence in ([("task",)], [("set_parameters", "capture_preview")], [("set_parameters",), ()]):
            preview = preview_fixture()
            replies = iter(sequence)

            async def send(
                _client: httpx2.AsyncClient, request: httpx2.Request, *, replies: Iterator[tuple[str, ...]] = replies, **_kwargs: object
            ) -> httpx2.Response:
                return completion(request, next(replies))

            with patch.dict(os.environ, {}, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
                agent = build_parameter_agent(preview=preview, model=ChatOpenAI(api_key="offline", model="offline-model"))
                with self.subTest(sequence=sequence), self.assertRaises(ParameterProtocolError):
                    await agent.ainvoke({"messages": [HumanMessage(content="adjust")]})
            preview.capture.assert_not_called()
