"""修复额度按稳定工作身份保存, 发送前拒绝及网络重试不重复消耗."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx2
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ToolCallRequest
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import tool
from langchain_openai import ChatOpenAI

from shader_deep.analysis.loop import AnalysisLoop
from shader_deep.analysis.submissions import SubmissionReply
from shader_deep.analysis.types import AnalysisExecution, AnalysisLimitError, ToolFeedback


@tool
def accept_count(count: int) -> str:
    """接收已校验的计数."""
    return str(count)


class ScopedRepairTests(TestCase):
    def setUp(self) -> None:
        self.execution = AnalysisExecution()
        self.scope = "outline"
        self.events: list[dict[str, object]] = []
        model = ChatOpenAI(model="fixture-model", api_key="fixture-key", base_url="https://shader-deep.invalid/v1")
        self.request = ModelRequest(model=model, messages=[HumanMessage(content="开始")], state={})
        self.handler = Mock(return_value=ModelResponse(result=[AIMessage(content="继续")]))

    def loop(self, **kwargs: object) -> AnalysisLoop:
        return AnalysisLoop(
            self.execution,
            20,
            lambda: HumanMessage(content="正文"),
            lambda: False,
            [accept_count],
            repair_scope=lambda: self.scope,
            **kwargs,
        )

    def reject(self, loop: AnalysisLoop, identity: str = "call-1") -> None:
        request = ToolCallRequest(
            tool_call={"name": "accept_count", "args": {"count": "bad"}, "id": identity, "type": "tool_call"}, tool=None, state={}, runtime=Mock()
        )
        self.assertEqual(loop.wrap_tool_call(request, Mock()).status, "error")

    def test_outline_repairs_do_not_consume_new_work_allowance(self) -> None:
        outline = self.loop()
        for _ in range(2):
            self.reject(outline)
            outline.wrap_model_call(self.request, self.handler)
        self.scope = "work-1"
        work = self.loop()
        self.reject(work)
        work.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 3)
        self.assertEqual(self.execution.format_repair_scopes, {"outline": 2, "work-1": 1})

    def test_same_work_rebuilding_and_resuming_cannot_reset_allowance(self) -> None:
        self.scope = "work-1"
        for _ in range(2):
            loop = self.loop()
            self.reject(loop)
            loop.wrap_model_call(self.request, self.handler)
        self.scope = "discovery"
        self.scope = "work-1"
        resumed = self.loop()
        self.reject(resumed)
        with self.assertRaisesRegex(AnalysisLimitError, "format-repair"):
            resumed.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.handler.call_count, 2)

    def test_failure_scope_is_captured_before_later_tools_change_work(self) -> None:
        self.scope = "work-1"
        loop = self.loop()
        self.reject(loop, "first")
        self.scope = "work-2"
        self.reject(loop, "second")
        loop.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_scopes, {"work-1": 1})
        self.assertEqual([item.repair_scope for item in self.execution.tool_feedback], ["work-1", "work-2"])

    def test_same_turn_success_resolves_old_errors_without_resetting_spent_allowance(self) -> None:
        for starting_scope in ("outline", "work-1"):
            with self.subTest(scope=starting_scope):
                self.execution = AnalysisExecution(format_repair_calls=2, format_repair_scopes={starting_scope: 2})
                self.scope = starting_scope
                loop = self.loop()
                self.reject(loop)
                loop.submission_handler = Mock()
                loop.submission_handler.handle.return_value = SubmissionReply(content='{"status":"submitted"}')
                request = ToolCallRequest(
                    tool_call={"name": "accept_count", "args": {"count": 1}, "id": "success", "type": "tool_call"},
                    tool=None,
                    state={},
                    runtime=Mock(),
                )
                loop.wrap_tool_call(request, Mock())
                self.scope = "next-work"
                self.loop().wrap_model_call(self.request, self.handler)
                self.assertEqual(self.execution.format_repair_calls, 2)
                self.scope = starting_scope
                resumed = self.loop()
                self.reject(resumed)
                with self.assertRaisesRegex(AnalysisLimitError, "format-repair"):
                    resumed.wrap_model_call(self.request, self.handler)

    def test_successful_scope_transition_retires_discovery_failure_without_reset(self) -> None:
        self.execution = AnalysisExecution(format_repair_calls=2, format_repair_scopes={"discovery": 2})
        self.scope = "discovery"
        loop = self.loop(repair_scope_active=lambda scope: scope == self.scope)
        self.reject(loop)
        self.scope = "work-1"
        loop.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 2)
        self.assertEqual(self.execution.format_repair_scopes, {"discovery": 2})
        self.assertEqual(self.execution.tool_feedback[0].repair_scope, "discovery")
        self.assertEqual(self.execution.format_repair_resolved_through, {})

    def test_current_work_failure_is_repaired_after_inactive_discovery_error(self) -> None:
        self.execution = AnalysisExecution(format_repair_calls=2, format_repair_scopes={"discovery": 2})
        self.scope = "discovery"
        loop = self.loop(repair_scope_active=lambda scope: scope == self.scope)
        self.reject(loop, "discovery")
        self.scope = "work-1"
        self.reject(loop, "work")
        loop.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 3)
        self.assertEqual(self.execution.format_repair_scopes, {"discovery": 2, "work-1": 1})

    def test_nontransitioning_success_does_not_retire_exhausted_discovery(self) -> None:
        self.execution = AnalysisExecution(format_repair_calls=2, format_repair_scopes={"discovery": 2})
        self.scope = "discovery"
        loop = self.loop(repair_scope_active=lambda scope: scope == self.scope)
        self.reject(loop)
        request = ToolCallRequest(
            tool_call={"name": "accept_count", "args": {"count": 1}, "id": "query", "type": "tool_call"}, tool=None, state={}, runtime=Mock()
        )
        loop.wrap_tool_call(request, lambda _request: ToolMessage(content="query succeeded", tool_call_id="query"))
        with self.assertRaisesRegex(AnalysisLimitError, "format-repair"):
            loop.wrap_model_call(self.request, self.handler)
        self.handler.assert_not_called()
        self.assertEqual(self.execution.format_repair_scopes, {"discovery": 2})

    def test_preflight_rejection_does_not_charge_repair(self) -> None:
        for late in (False, True):
            with self.subTest(late=late):
                self.execution = AnalysisExecution()
                checks = Mock(side_effect=[None, AnalysisLimitError("context")] if late else AnalysisLimitError("context"))
                presented = Mock()
                loop = self.loop(before_request=checks, on_prepared=presented)
                self.reject(loop)
                with self.assertRaisesRegex(AnalysisLimitError, "context"):
                    loop.wrap_model_call(self.request, self.handler)
                self.assertEqual(self.execution.format_repair_calls, 0)
                self.assertEqual(self.execution.format_repair_scopes, {})
                presented.assert_not_called()
        self.handler.assert_not_called()

    def test_network_retry_charges_once_and_output_limit_never_charges(self) -> None:
        loop = self.loop(request_retries=1)
        self.reject(loop)
        self.handler.side_effect = [
            httpx2.ConnectError("transient"),
            ModelResponse(result=[AIMessage(content="", response_metadata={"finish_reason": "length"})]),
        ]
        with patch("shader_deep.analysis.transport.time.sleep"):
            loop.wrap_model_call(self.request, self.handler)
        self.handler.side_effect = None
        loop.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 1)
        self.assertEqual(len(self.execution.request_attempts), 3)

    def test_legacy_unlimited_mode_remains_unlimited(self) -> None:
        loop = AnalysisLoop(self.execution, 0, lambda: HumanMessage(content="正文"), lambda: False, [accept_count])
        for _ in range(4):
            self.reject(loop)
            loop.wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 4)

    def test_history_preparation_precedes_context_and_budget(self) -> None:
        events: list[str] = []
        loop = self.loop(
            prepare_history=lambda _messages: [HumanMessage(content="压缩后")],
            before_request=lambda request: events.append(str(request.messages[0].content)),
        )
        loop.wrap_model_call(self.request, self.handler)
        self.assertIn("压缩后", events[0])
        self.assertEqual(self.request.messages[0].content, "开始")

    def test_full_tool_artifact_and_short_result_distinguish_partial_and_rejected(self) -> None:
        loop = self.loop(on_tool_result=self.events.append)
        request = ToolCallRequest(
            tool_call={"name": "accept_count", "args": {"count": 1}, "id": "read", "type": "tool_call"}, tool=None, state={}, runtime=Mock()
        )
        reply = ToolMessage(content=json.dumps({"status": "selected", "not_provided_ids": ["x"]}), tool_call_id="read")
        loop.wrap_tool_call(request, lambda _request: reply)
        self.reject(loop)
        self.assertEqual([event["outcome"] for event in self.events], ["partial", "rejected"])
        self.assertEqual(self.events[0]["arguments"], {"count": 1})
        self.assertEqual(self.events[0]["response"], reply.content)

    def test_business_errors_do_not_consume_format_allowance(self) -> None:
        self.execution.tool_feedback = (ToolFeedback(model_call=0, tool="read_library", message="budget", category="business_rejection"),)
        self.loop().wrap_model_call(self.request, self.handler)
        self.assertEqual(self.execution.format_repair_calls, 0)

    def test_parallel_tools_keep_their_captured_scope_for_feedback_and_success(self) -> None:
        for first_success in (False, True):
            with self.subTest(first_success=first_success):
                self._assert_parallel_scopes(first_success=first_success)

    def _assert_parallel_scopes(self, *, first_success: bool) -> None:
        self.execution = AnalysisExecution()
        self.events.clear()
        entered, completed = Event(), Event()
        identity = local()
        loop = self.loop(on_tool_result=self.events.append)
        loop.repair_scope = lambda: identity.scope

        def submit(_name: str, arguments: dict[str, object]) -> SubmissionReply:
            first = arguments["count"] == 1
            if first:
                entered.set()
                self.assertTrue(completed.wait(5))
            return SubmissionReply(
                content="accepted" if first == first_success else "schema error",
                category=None if first == first_success else "schema_validation",
            )

        loop.submission_handler = Mock()
        loop.submission_handler.handle.side_effect = submit

        def invoke(number: int) -> None:
            identity.scope = f"work-{number}"
            request = ToolCallRequest(
                tool_call={"name": "accept_count", "args": {"count": number}, "id": identity.scope, "type": "tool_call"},
                tool=None,
                state={},
                runtime=Mock(),
            )
            loop.wrap_tool_call(request, Mock())
            if number != 1:
                completed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke, 1)
            self.assertTrue(entered.wait(5))
            second = pool.submit(invoke, 2)
            second.result(timeout=5)
            first.result(timeout=5)
        failed_scope = "work-2" if first_success else "work-1"
        success_scope = "work-1" if first_success else "work-2"
        self.assertEqual(self.execution.tool_feedback[0].repair_scope, failed_scope)
        self.assertEqual(set(self.execution.format_repair_resolved_through), {success_scope})
        self.assertEqual({event["call_id"]: event["scope"] for event in self.events}, {"work-1": "work-1", "work-2": "work-2"})
