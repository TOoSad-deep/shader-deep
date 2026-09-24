"""验证整合请求预算与动态工具权限, 不连接真实模型服务."""

from __future__ import annotations

from dataclasses import replace

from langchain.agents.middleware import ModelRequest
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.budgets import RequestBudgetError, check_request, estimated_tokens, remaining_material_chars
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.workflows.configuration import load_analysis_options
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results


@tool
def inspect_fragment(description: str) -> str:
    """接收材料描述, 用于验证工具参数结构计入请求预算.

    Args:
        description: 需要检查的材料描述.
    """
    return description


class IntegrationBudgetTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.budget = AnalysisOptions(
            max_context_tokens=200, max_output_tokens=20, request_token_margin=1, request_image_tokens=100, integration_history_tokens=100
        )

    def test_full_request_checks_system_history_images_and_tool_schema(self) -> None:
        request = ModelRequest(model=build_model(), messages=[HumanMessage(content="材料")])
        check_request(request, [], self.budget)
        oversized = (
            request.override(system_message=SystemMessage(content="指令" * 120)),
            request.override(messages=[HumanMessage(content="材料"), ToolMessage(content="修复反馈" * 80, tool_call_id="prior")]),
            request.override(messages=[HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}}] * 2)]),
            request.override(
                messages=[AIMessage(content="", tool_calls=[{"name": "inspect_fragment", "id": "call", "args": {"description": "旧参数" * 80}}])]
            ),
        )
        for candidate in oversized:
            with self.subTest(messages=candidate.messages), self.assertRaises(RequestBudgetError):
                check_request(candidate, [], self.budget)
        with self.assertRaises(RequestBudgetError):
            check_request(request, [inspect_fragment], self.budget)

    def test_output_reservation_and_image_count_reduce_material_capacity(self) -> None:
        options = replace(self.budget, max_context_tokens=10000)
        context = HumanMessage(content=[{"type": "text", "text": "初稿"}])
        with_image = context.model_copy(
            update={"content": [*context.content, {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}}]}
        )
        plain = remaining_material_chars("角色指令", context, [], options)
        image = remaining_material_chars("角色指令", with_image, [], options)
        self.assertEqual(plain - image, options.request_image_tokens)
        self.assertLess(remaining_material_chars("角色指令", context, [inspect_fragment], options), plain)
        more_output = replace(options, max_output_tokens=options.max_output_tokens + 500)
        self.assertEqual(remaining_material_chars("角色指令", context, [], more_output), plain - 500)
        more_history = replace(options, integration_history_tokens=options.integration_history_tokens + 300)
        self.assertEqual(remaining_material_chars("角色指令", context, [], more_history), plain - 300)
        self.assertGreater(estimated_tokens(20, 1, options), estimated_tokens(20, 0, options))

    def test_budget_rejection_prevents_transport_and_presentation(self) -> None:
        execution = AnalysisExecution()
        presented: list[object] = []
        loop = AnalysisLoop(
            execution,
            3,
            lambda: HumanMessage(content="完整材料" * 100),
            lambda: False,
            [inspect_fragment],
            before_request=lambda request: check_request(request, [inspect_fragment], self.budget),
            on_prepared=presented.extend,
            role_prompt_only=True,
        )
        with self.assertRaises(RequestBudgetError):
            loop.run(configure_analysis_model(build_model(), self.budget), "角色指令", {"recursion_limit": 20})
        self.assertEqual(self.requests, [])
        self.assertEqual(presented, [])
        self.assertEqual(execution.model_calls, 0)
        self.assertEqual(execution.request_attempts, ())

    def test_exhausted_tool_is_hidden_and_rejected_even_if_model_calls_it(self) -> None:
        performed: list[str] = []
        completed: list[bool] = []
        rejected_attempt = 2

        @tool
        def consume_measurement() -> str:
            """消耗唯一测量机会."""
            performed.append("measured")
            return '{"status":"measured"}'

        @tool
        def complete_package() -> str:
            """提交本包完成决定."""
            completed.append(True)
            return '{"status":"completed"}'

        def respond(request: dict[str, object]) -> dict[str, object]:
            exposed = {item["function"]["name"] for item in request["tools"]}
            if len(self.requests) == 1:
                self.assertIn("consume_measurement", exposed)
                return self.call("consume_measurement", {})
            self.assertNotIn("consume_measurement", exposed)
            if len(self.requests) == rejected_attempt:
                return self.call("consume_measurement", {})
            self.assertEqual(tool_results(request)[-1]["status"], "tool_error")
            return self.call("complete_package", {})

        self.response = respond
        execution = AnalysisExecution()
        loop = AnalysisLoop(
            execution,
            4,
            lambda: HumanMessage(content="当前材料"),
            lambda: bool(completed),
            [consume_measurement, complete_package],
            available_tools=lambda: [complete_package] if performed else [consume_measurement, complete_package],
            role_prompt_only=True,
        )
        loop.run(configure_analysis_model(build_model(), self.budget), "角色指令", {"recursion_limit": 30})
        self.assertEqual(performed, ["measured"])
        self.assertEqual(completed, [True])
        self.assertEqual(execution.model_calls, 3)
        self.assertEqual(execution.tool_feedback[-1].category, "permission")

    def test_new_budget_configuration_requires_positive_integers(self) -> None:
        fields = (
            "max_context_tokens",
            "request_image_tokens",
            "request_token_margin",
            "max_integration_calls",
            "max_integration_packages",
            "integration_no_progress",
            "integration_history_tokens",
        )
        for field in fields:
            for invalid in (0, -1, True, 1.5, "100"):
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ValueError, field):
                    AnalysisOptions(**{field: invalid})
        configuration = self.root / "budget.yaml"
        configuration.write_text("\n".join(f"{field}: {index + 1}" for index, field in enumerate(fields)))
        configured = load_analysis_options(configuration)
        for index, field in enumerate(fields):
            self.assertEqual(getattr(configured, field), index + 1)
