"""无网络模型通过真实 SDK 图验证全图规划与草稿能力."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image
from pydantic import Field

from shader_deep.agents.planning.native import FixedPlanningInput, build_planning_agent, planning_input
from shader_deep.domain.scene_observations import ObservationIssue, SceneObservationSubmission, build_scene_observations
from shader_deep.infrastructure.storage.observation_package import ObservationManifest, ObservationReference, write_observation_package
from shader_deep.infrastructure.storage.planning_inputs import capture_planning_source
from shader_deep.workflows.planning.options import PlanningOptions
from shader_deep.workflows.planning.session import PlanningSession

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.runnables import Runnable


def observation_package(directory: Path, *, gaps: tuple[ObservationIssue, ...] = ()) -> Path:
    reference = io.BytesIO()
    Image.new("RGB", (8, 5), (40, 90, 150)).save(reference, format="PNG")
    content = reference.getvalue()
    submission = SceneObservationSubmission.model_validate(
        {
            "elements": [
                {"id": "E1", "name": "蓝圆", "region": "左下", "features": [{"id": "F1", "description": "蓝色圆形"}]},
                {"id": "E2", "name": "浅圆", "region": "右上", "features": [{"id": "F2", "description": "浅灰圆形"}]},
            ],
            "relations": [{"id": "R1", "participants": ["E1", "E2"], "description": "浅圆覆盖蓝圆右上方"}],
            "context_notes": ["浅灰背景"],
            "gaps": [item.model_dump(mode="json") for item in gaps],
        }
    )
    manifest = ObservationManifest(
        reference=ObservationReference(sha256=hashlib.sha256(content).hexdigest()),
        status="partial" if gaps else "completed",
        context_notes=submission.context_notes,
        gaps=submission.gaps,
    )
    return write_observation_package(directory, build_scene_observations(submission), manifest, content)


def planning() -> dict[str, object]:
    return {
        "mechanisms": [
            {"id": "M1", "name": "圆形遮罩填充", "method": "依据中心与半径求圆形覆盖率, 混合表面色和背景色输出颜色, 中心、半径及边缘宽度可调."}
        ],
        "sketches": [
            {
                "id": "S1",
                "name": "全图圆形方案",
                "default": {"F1": ["M1"], "F2": ["M1"], "R1": ["M1"]},
                "composition": "两次应用 [[M1]] 构造 [[E1]] 和 [[E2]], 后者位于右上并覆盖前者.",
            }
        ],
    }


def call(name: str, args: dict[str, object], identity: str = "call") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identity}])


def fixed_context(messages: list[BaseMessage]) -> dict[str, object]:
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    payload = json.loads(str(block["text"]))
                    if isinstance(payload, dict) and "observation_sha256" in payload:
                        return payload
    msg = "实际请求缺少固定规划输入"
    raise AssertionError(msg)


class PlanningModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    requests: list[list[BaseMessage]] = Field(default_factory=list)
    offered_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scene-planner-offline-test"

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Runnable[LanguageModelInput, BaseMessage]:
        del kwargs
        self.offered_tools = [str(getattr(item, "name", "")) for item in tools]
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object) -> ChatResult:
        del stop, run_manager, kwargs
        self.requests.append(list(messages))
        index = len(self.requests) - 1
        payload = fixed_context(messages)
        revision = payload.get("draft_revision")
        if index < len(self.responses):
            response = self.responses[index]
        elif revision is None:
            response = call("save_planning_draft", {"report": planning()}, str(index))
        elif any(isinstance(item, ToolMessage) and f"Draft revision: {revision}" in str(item.content) for item in messages):
            response = call("finish_planning", {}, str(index))
        else:
            response = call("read_file", {"file_path": "/draft/planning.json"}, str(index))
        return ChatResult(generations=[ChatGeneration(message=response)])


class NativePlanningTests(unittest.TestCase):
    def make_session(self, *, max_model_calls: int = 20, gaps: tuple[ObservationIssue, ...] = ()) -> PlanningSession:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        report = observation_package(directory / "observation", gaps=gaps)
        source = capture_planning_source(report, directory / "run", "规划全图两个圆的实现")
        return PlanningSession(source, PlanningOptions(max_request_retries=0, max_model_calls=max_model_calls))

    def invoke(self, model: PlanningModel, session: PlanningSession) -> dict[str, object]:
        return build_planning_agent(model, session, session.options).invoke(planning_input(session))

    def test_one_planner_sees_fixed_whole_scene_and_finishes(self) -> None:
        session, model = self.make_session(), PlanningModel()
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(set(model.offered_tools), {"save_planning_draft", "read_file", "edit_file", "finish_planning", "stop_planning"})
        for messages in model.requests:
            payload = fixed_context(messages)
            self.assertEqual(payload["observations"]["relations"][0]["id"], "R1")
            self.assertEqual(payload["generation_constraints"]["width"], 8)
            images = [
                block
                for item in messages
                if isinstance(item.content, list)
                for block in item.content
                if isinstance(block, dict) and block.get("type") == "image_url"
            ]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]["image_url"]["url"], session.reference_url)
        self.assertEqual(session.accepted_revision, session.seen_revision)
        self.assertTrue((session.directory / "results/planning.json").is_file())

    def test_local_reference_repair_and_dependent_edits(self) -> None:
        session = self.make_session()
        report = planning()
        report["sketches"][0]["default"]["R99"] = report["sketches"][0]["default"].pop("R1")
        batch = AIMessage(
            content="",
            tool_calls=[
                {"name": "edit_file", "args": {"file_path": "/draft/planning.json", "old_string": '"R99"', "new_string": '"R1"'}, "id": "edit1"},
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/planning.json", "old_string": "全图圆形方案", "new_string": "共享圆形方案"},
                    "id": "edit2",
                },
                {"name": "read_file", "args": {"file_path": "/draft/planning.json"}, "id": "read"},
            ],
        )
        result = self.invoke(PlanningModel(responses=[call("save_planning_draft", {"report": report}), batch]), session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.submission.sketches[0].name, "共享圆形方案")
        self.assertEqual(len(list((session.directory / "draft/history").glob("*.json"))), 2)
        self.assertTrue(all(item.status == "success" for item in result["messages"] if isinstance(item, ToolMessage)))

    def test_upstream_global_gap_survives_full_target_mapping(self) -> None:
        session = self.make_session(gaps=(ObservationIssue(description="全图边界需要更清晰输入"),))
        self.invoke(PlanningModel(), session)
        self.assertEqual(session.finalize().status, "partial")
        self.assertEqual(session.context()["observation_gaps"][0]["refs"], [])

    def test_read_removed_from_actual_request_cannot_finish(self) -> None:
        session = self.make_session(max_model_calls=4)
        prepare = FixedPlanningInput._prepare

        def filtered(middleware: FixedPlanningInput, request: ModelRequest) -> ModelRequest:
            result = prepare(middleware, request)
            return result.override(messages=[item for item in result.messages if not isinstance(item, ToolMessage)])

        model = PlanningModel(
            responses=[
                call("save_planning_draft", {"report": planning()}),
                call("read_file", {"file_path": "/draft/planning.json"}, "read"),
                call("finish_planning", {}, "finish"),
                call("stop_planning", {"reason": "未见回读"}, "stop"),
            ]
        )
        with patch.object(FixedPlanningInput, "_prepare", filtered):
            self.invoke(model, session)
        self.assertEqual(session.finalize().status, "failed")
        self.assertIsNone(session.seen_revision)
        self.assertTrue(session.draft.path.exists())

    def test_tool_permission_and_terminal_stop(self) -> None:
        session = self.make_session()
        model = PlanningModel(
            responses=[
                call("task", {"subagent_type": "general-purpose", "description": "delegate"}),
                call("edit_file", {"file_path": "/inputs/request.txt", "old_string": "规划", "new_string": "改写"}, "edit"),
                call("read_file", {"file_path": "/etc/passwd"}, "host"),
                call("stop_planning", {"reason": "需要补充输入"}, "stop"),
            ]
        )
        result = self.invoke(model, session)
        errors = [item for item in result["messages"] if isinstance(item, ToolMessage) and item.status == "error"]
        self.assertEqual(len(errors), 3)
        self.assertEqual(len(model.requests), 4)
        self.assertEqual(session.finalize().status, "failed")
        self.assertEqual(session.reason, "需要补充输入")

    def test_reference_is_progressively_disclosed(self) -> None:
        session = self.make_session()
        path = "/skills/scene-planner/references/alternatives.md"
        model = PlanningModel(responses=[call("read_file", {"file_path": path})])
        self.invoke(model, session)
        text = "候选索引保留所有来源用于回看"
        self.assertNotIn(text, str(model.requests[0]))
        self.assertIn(text, str(model.requests[1]))
        self.assertEqual(session.resource_read_results, [{"path": path, "status": "success"}])

    def test_budget_and_plain_text_keep_unaccepted_draft(self) -> None:
        session = self.make_session(max_model_calls=1)
        self.invoke(PlanningModel(), session)
        self.assertEqual(session.finalize().status, "failed")
        self.assertEqual(session.reason, "model_call_limit")
        self.assertTrue(session.draft.path.exists())
        another = self.make_session()
        self.invoke(PlanningModel(responses=[AIMessage(content="规划已完成")]), another)
        self.assertEqual(another.finalize().status, "failed")

    def test_paginated_read_and_finish_wait_for_next_response(self) -> None:
        session = self.make_session()
        report = planning()
        original = report["sketches"][0]
        report["sketches"].extend({**original, "id": f"S{number}"} for number in range(2, 12))
        pages = AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "args": {"file_path": "/draft/planning.json", "limit": 100}, "id": "page1"},
                {"name": "read_file", "args": {"file_path": "/draft/planning.json", "offset": 100, "limit": 2000}, "id": "page2"},
                {"name": "finish_planning", "args": {}, "id": "early"},
            ],
        )
        model = PlanningModel(responses=[call("save_planning_draft", {"report": report}), pages])
        result = self.invoke(model, session)
        finishes = [item for item in result["messages"] if isinstance(item, ToolMessage) and item.name == "finish_planning"]
        self.assertEqual([item.status for item in finishes], ["error", "success"])
        self.assertIn("next offset 100", str(model.requests[-1]))
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.seen_revision, session.accepted_revision)


class AsyncNativePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_dependent_edits_are_read_before_finish(self) -> None:
        helper = NativePlanningTests()
        self.addCleanup(helper.doCleanups)
        session = helper.make_session()
        batch = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/planning.json", "old_string": "全图圆形方案", "new_string": "共享圆形方案"},
                    "id": "edit",
                },
                {"name": "read_file", "args": {"file_path": "/draft/planning.json"}, "id": "read"},
            ],
        )
        model = PlanningModel(responses=[call("save_planning_draft", {"report": planning()}), batch])
        await build_planning_agent(model, session, session.options).ainvoke(planning_input(session))
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.submission.sketches[0].name, "共享圆形方案")
