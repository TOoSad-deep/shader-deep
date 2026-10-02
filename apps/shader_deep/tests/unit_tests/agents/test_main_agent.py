"""持续主 Agent 的真实 SDK 图、技能读取与跨响应工具权限验证."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from shader_deep.agents.main.agent import run_main_agent
from shader_deep.agents.main.skill import SKILL_PATH, OrchestrationSkill
from shader_deep.infrastructure.llm.transport import RAW_TOOL_CALLS
from shader_deep.runtime.budgets import RequestBudgetError
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError, AnalysisNoProgressError
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.options import AnalysisOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import BaseMessage

    from shader_deep.domain.five_libraries import Element
    from shader_deep.runtime.task_store import Attempt, JsonValue


ELEMENTS = {"elements": [{"id": "E1", "name": "圆形", "region": "画面中央", "feature_ids": []}]}
DISPATCH = {"target_element_id": "E1", "directions": ["轮廓与边界", "颜色与明暗", "空间与局部细节"]}
READ_SKILL = ("read_skill", {"path": SKILL_PATH})
NORMAL = [
    [READ_SKILL],
    [("submit_elements", ELEMENTS)],
    [("dispatch_exploration", DISPATCH)],
    [("read_analysis_result", {"result_id": "R1"})],
    [("dispatch_integration", {})],
    [("read_analysis_result", {"result_id": "V1"})],
    [("finish_analysis", {})],
]


class ScriptedMainModel(BaseChatModel):
    """发送故意夹带调用的真实模型响应, 记录实际材料和可见工具."""

    scripts: list[list[tuple[str, dict[str, object]]]]
    requests: list[list[object]] = Field(default_factory=list)
    tool_sets: list[tuple[str, ...]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-main-agent"

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> object:
        del kwargs
        return self.bind(allowed_tools=tuple(getattr(item, "name", "") for item in tools))

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object) -> ChatResult:
        del stop, run_manager
        number = len(self.requests)
        self.requests.append(list(messages))
        self.tool_sets.append(kwargs.get("allowed_tools", ()))
        selected = self.scripts[number] if number < len(self.scripts) else []
        calls = [{"name": name, "args": arguments, "id": f"call-{number}-{index}"} for index, (name, arguments) in enumerate(selected)]
        raw = [{"name": call["name"], "args": json.dumps(call["args"]), "id": call["id"]} for call in calls]
        response = AIMessage(
            content="" if calls else "继续思考",
            tool_calls=calls,
            additional_kwargs={RAW_TOOL_CALLS: raw},
            response_metadata={"finish_reason": "tool_calls" if calls else "stop"},
        )
        return ChatResult(generations=[ChatGeneration(message=response)])


class RecordingActions:
    """保存可观察业务动作, 最终提交前 main attempt 始终保持运行."""

    def __init__(self, store: TaskStore, attempt: Attempt) -> None:
        self.store, self.attempt = store, attempt
        self.elements: tuple[Element, ...] = ()
        self.dispatched = False
        self.integrated = False
        self.read_ids: set[str] = set()
        self.calls: list[str] = []
        self.statuses: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        self.statuses.append(str(self.store.task("main")["status"]))

    def submit_elements(self, elements: tuple[Element, ...]) -> dict[str, JsonValue]:
        self._record("submit_elements")
        self.elements = elements
        return {"status": "accepted", "elements": [item.model_dump(mode="json") for item in elements]}

    def dispatch_exploration(self, target_element_id: str, directions: tuple[str, ...]) -> dict[str, JsonValue]:
        if target_element_id not in {item.id for item in self.elements} or len(directions) != len(DISPATCH["directions"]):
            msg = "无效目标或方向"
            raise ValueError(msg)
        self._record("dispatch_exploration")
        self.dispatched = True
        return {"status": "completed", "result_ids": ["R1"]}

    def read_analysis_result(self, result_id: str) -> dict[str, JsonValue]:
        self._record("read_analysis_result")
        self.read_ids.add(result_id)
        return {"status": "read", "result_id": result_id, "observation": "中央可见圆形边界"}

    def dispatch_integration(self) -> dict[str, JsonValue]:
        self._record("dispatch_integration")
        self.integrated = True
        return {"status": "completed", "result_id": "V1"}

    def finish_analysis(self) -> dict[str, JsonValue]:
        self._record("finish_analysis")
        if not self.integrated:
            msg = "尚未整合"
            raise ValueError(msg)
        self.store.submit(self.attempt, "main-final", {"status": "completed"})
        return {"status": "accepted"}

    def allowed_tools(self) -> tuple[str, ...]:
        allowed = ("submit_elements", "finish_analysis")
        if self.elements:
            allowed += ("dispatch_exploration",)
        if self.dispatched:
            allowed += ("read_analysis_result", "dispatch_integration")
        return allowed

    def progress(self) -> object:
        return self.elements, self.dispatched, self.integrated, tuple(sorted(self.read_ids))

    def context(self) -> dict[str, JsonValue]:
        return {"registered_elements": [item.model_dump(mode="json") for item in self.elements]}

    def done(self) -> bool:
        return self.store.task("main")["status"] == "succeeded"


class MainAgentTests(unittest.TestCase):
    def run_case(
        self,
        scripts: list[list[tuple[str, dict[str, object]]]],
        options: AnalysisOptions | None = None,
        *,
        request: str = "分析圆形",
    ) -> tuple[ScriptedMainModel, RecordingActions, AnalysisExecution, TaskStore]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = self.enterContext(TaskStore(directory))
        store.register("main", "fixed-input", {})
        attempt = store.start_attempt("main")
        model = ScriptedMainModel(scripts=scripts)
        actions, execution = RecordingActions(store, attempt), AnalysisExecution()
        self.model, self.actions, self.execution, self.store = model, actions, execution, store
        run_main_agent(store, attempt, model, options or AnalysisOptions(), execution, request, "data:image/png;base64,AA==", actions)
        return model, actions, execution, store

    def test_actual_skill_metadata_full_content_and_continuous_business(self) -> None:
        model, actions, execution, store = self.run_case(NORMAL)
        skill = OrchestrationSkill()
        self.assertEqual(model.tool_sets[0], ("read_skill",))
        self.assertIn(SKILL_PATH, str(model.requests[0][0].content))
        self.assertEqual(str(model.requests[0][0].content).count("可用编排 skill"), 1)
        self.assertNotIn("read_file", str(model.requests[0][0].content))
        skill_receipt = next(item for item in model.requests[1] if isinstance(item, ToolMessage) and item.name == "read_skill")
        self.assertEqual(json.loads(skill_receipt.content)["content"], skill.content)
        self.assertIn("dispatch_exploration", model.tool_sets[2])
        self.assertEqual(execution.model_calls, 7)
        self.assertEqual(store.task("main")["model_calls"], 7)
        self.assertEqual(execution.status, "completed")
        self.assertEqual(actions.statuses, ["running"] * 6)
        self.assertEqual(actions.calls[-1], "finish_analysis")
        self.assertEqual(store.task("main")["status"], "succeeded")

    def test_same_response_dispatch_rejected_then_next_response_accepted(self) -> None:
        scripts = [NORMAL[0], [("submit_elements", ELEMENTS), ("dispatch_exploration", DISPATCH)], *NORMAL[2:]]
        model, actions, execution, _ = self.run_case(scripts)
        self.assertNotIn("dispatch_exploration", model.tool_sets[1])
        errors = [item for item in model.requests[2] if isinstance(item, ToolMessage) and item.status == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("尚未开放", str(errors[0].content))
        self.assertIn("dispatch_exploration", model.tool_sets[2])
        self.assertEqual(actions.calls.count("dispatch_exploration"), 1)
        self.assertEqual(execution.format_repair_calls, 0)

    def test_skill_read_and_business_same_response_cannot_bypass_bootstrap(self) -> None:
        model, actions, _, _ = self.run_case([[READ_SKILL, ("submit_elements", ELEMENTS)], *NORMAL[1:]])
        errors = [item for item in model.requests[1] if isinstance(item, ToolMessage) and item.status == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(actions.calls.count("submit_elements"), 1)

    def test_framework_task_and_filesystem_tools_are_rejected(self) -> None:
        scripts = [
            NORMAL[0],
            [
                ("task", {"description": "分析图片", "subagent_type": "general-purpose"}),
                ("read_file", {"file_path": "/etc/passwd"}),
                ("submit_elements", ELEMENTS),
            ],
            *NORMAL[2:],
        ]
        model, actions, _, _ = self.run_case(scripts)
        errors = [item for item in model.requests[2] if isinstance(item, ToolMessage) and item.status == "error"]
        self.assertEqual(len(errors), 2)
        for names in model.tool_sets:
            self.assertNotIn("task", names)
            self.assertNotIn("read_file", names)
        self.assertEqual(actions.calls.count("submit_elements"), 1)

    def test_missing_packaged_skill_prevents_model_and_business(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch("shader_deep.agents.main.skill.files", return_value=directory), self.assertRaises(FileNotFoundError):
            self.run_case(NORMAL)
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.actions.calls, [])

    def test_independent_read_tools_execute_serially(self) -> None:
        active = maximum = 0
        lock = threading.Lock()
        original = RecordingActions.read_analysis_result

        def observed(actions: RecordingActions, result_id: str) -> dict[str, JsonValue]:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                threading.Event().wait(0.02)
                return original(actions, result_id)
            finally:
                with lock:
                    active -= 1

        scripts = [*NORMAL[:3], [("read_analysis_result", {"result_id": "R1"}), ("read_analysis_result", {"result_id": "R2"})], *NORMAL[4:]]
        with patch.object(RecordingActions, "read_analysis_result", observed):
            _, actions, _, _ = self.run_case(scripts)
        self.assertEqual(maximum, 1)
        self.assertEqual(actions.read_ids, {"R1", "R2", "V1"})

    def test_large_accepted_report_enters_actual_request_without_package_write(self) -> None:
        original = RecordingActions.read_analysis_result
        body = "完整观察内容" * 15000 + "报告末尾证据"

        def large_result(actions: RecordingActions, result_id: str) -> dict[str, JsonValue]:
            payload = original(actions, result_id)
            if result_id == "R1":
                payload["observation"] = body
            return payload

        with (
            patch.object(RecordingActions, "read_analysis_result", large_result),
            patch.object(FilesystemBackend, "write", side_effect=AssertionError("业务制品不能写入安装包")) as write,
        ):
            model, _, _, _ = self.run_case(NORMAL)
        receipt = next(item for item in model.requests[4] if isinstance(item, ToolMessage) and item.name == "read_analysis_result")
        self.assertEqual(json.loads(receipt.content)["observation"], body)
        write.assert_not_called()
        self.assertNotIn("read_file", model.tool_sets[4])

    def test_large_human_context_is_complete_and_never_written_to_package(self) -> None:
        request = "用户要求内容" * 35000 + "要求末尾证据"
        with patch.object(FilesystemBackend, "write", side_effect=AssertionError("用户材料不能写入安装包")) as write:
            model, _, _, _ = self.run_case(NORMAL, AnalysisOptions(max_context_tokens=500000), request=request)
        for messages in model.requests:
            self.assertIn("要求末尾证据", str(messages))
        write.assert_not_called()

    def test_large_readback_still_obeys_real_request_budget(self) -> None:
        original = RecordingActions.read_analysis_result

        def large_result(actions: RecordingActions, result_id: str) -> dict[str, JsonValue]:
            return {**original(actions, result_id), "observation": "完整观察内容" * 15000}

        with (
            patch.object(RecordingActions, "read_analysis_result", large_result),
            patch.object(FilesystemBackend, "write", side_effect=AssertionError("不能通过落盘绕过实际预算")) as write,
            self.assertRaises(RequestBudgetError),
        ):
            self.run_case(NORMAL, AnalysisOptions(max_context_tokens=80000))
        self.assertEqual(len(self.model.requests), 4)
        self.assertNotIn("dispatch_integration", self.actions.calls)
        write.assert_not_called()

    def test_schema_feedback_allows_next_response_to_repair_registration(self) -> None:
        model, actions, execution, store = self.run_case([NORMAL[0], [("submit_elements", {"elements": [{}]})], *NORMAL[1:]])
        self.assertEqual(actions.calls.count("submit_elements"), 1)
        self.assertNotIn("dispatch_exploration", model.tool_sets[2])
        self.assertEqual(store.task("main")["repairs"], 1)
        self.assertEqual(execution.format_repair_calls, 1)

    def test_wrong_skill_path_never_opens_business_and_exhausts_repairs(self) -> None:
        scripts = [[("read_skill", {"path": "/etc/passwd"})]] * 4
        with self.assertRaises(AnalysisNoProgressError):
            self.run_case(scripts)
        self.assertEqual(self.actions.calls, [])
        self.assertTrue(all(names == ("read_skill",) for names in self.model.tool_sets))
        self.assertEqual(self.store.task("main")["repairs"], 2)

    def test_main_budget_is_persisted_and_stops_before_dispatch(self) -> None:
        with self.assertRaises(AnalysisLimitError):
            self.run_case(NORMAL, AnalysisOptions(max_main_calls=2))
        self.assertEqual(self.actions.calls, ["submit_elements"])
        self.assertEqual(self.store.task("main")["model_calls"], 2)
        self.assertEqual(self.store.task("main")["status"], "running")

    def test_repeated_idle_responses_have_a_finite_limit(self) -> None:
        with self.assertRaises(AnalysisNoProgressError):
            self.run_case([[READ_SKILL], [], [], [], [], []])
        self.assertEqual(self.actions.calls, [])
        self.assertLessEqual(len(self.model.requests), 5)
