"""无网络模型通过真实 SDK 图验证单角色观察的提交与权限."""

from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from deepagents.backends.utils import create_file_data
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image
from pydantic import Field

from shader_deep.agents.analysis.native import FixedInput, analysis_input, build_analysis_agent
from shader_deep.domain.scene_observations import SceneObservationSubmission
from shader_deep.workflows.analysis.session import AnalysisSession
from shader_deep.workflows.options import AnalysisOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.runnables import Runnable


def observation() -> dict[str, object]:
    return {
        "context_notes": ["浅灰背景"],
        "elements": [
            {"id": "E1", "name": "蓝色圆", "region": "左下", "features": [{"id": "F8", "description": "青蓝渐变"}]},
            {"id": "E3", "name": "浅色圆", "region": "右上", "features": [{"id": "F11", "description": "浅灰圆形边界"}]},
        ],
        "relations": [{"id": "R2", "participants": ["E1", "F11"], "description": "浅色圆覆盖蓝色圆的右上方"}],
    }


def call(name: str, args: dict[str, object], number: int = 1) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{number}"}])


def context(messages: list[BaseMessage]) -> dict[str, object]:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage) or not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = json.loads(str(block["text"]))
                if isinstance(value, dict) and "user_request" in value:
                    return value
    msg = "实际请求未提供固定上下文"
    raise AssertionError(msg)


class NativeRoutingModel(BaseChatModel):
    """按响应序列执行, 默认通过真实提交工具交付多元素观察."""

    responses: list[AIMessage] = Field(default_factory=list)
    requests: list[list[BaseMessage]] = Field(default_factory=list)
    offered_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scene-observer-offline-test"

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Runnable[LanguageModelInput, BaseMessage]:
        del kwargs
        self.offered_tools = [str(getattr(tool, "name", "")) for tool in tools]
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object) -> ChatResult:
        del stop, run_manager, kwargs
        self.requests.append(list(messages))
        index = len(self.requests) - 1
        if index < len(self.responses):
            response = self.responses[index]
        elif context(messages).get("draft_revision") is None:
            response = call("save_observation_draft", {"report": observation()}, index + 1)
        elif not any(
            isinstance(m, ToolMessage)
            and "@@ lines" in str(m.content)
            and m.tool_call_id
            in {
                c["id"]
                for entry in messages
                if isinstance(entry, AIMessage)
                for c in entry.tool_calls
                if c["name"] == "read_file" and c["args"].get("file_path") == "/draft/observations.json"
            }
            for m in messages
        ):
            response = call("read_file", {"file_path": "/draft/observations.json"}, index + 1)
        else:
            response = call("finish_analysis", {}, index + 1)
        return ChatResult(generations=[ChatGeneration(message=response)])


class NativeAnalysisTests(unittest.TestCase):
    def make_session(self, **values: object) -> AnalysisSession:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        buffer = io.BytesIO()
        Image.new("RGB", (5, 3), (240, 80, 140)).save(buffer, format="PNG")
        reference = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        options = AnalysisOptions(max_request_retries=0, **values)
        return AnalysisSession("分析两个圆形的全图关系", reference, directory, options)

    def invoke(self, model: NativeRoutingModel, session: AnalysisSession, initial: dict[str, object] | None = None) -> dict[str, object]:
        return build_analysis_agent(model, session, session.options).invoke(initial or analysis_input(session))

    def test_one_role_sees_one_fixed_image_and_submits_cross_element_relations(self) -> None:
        session = self.make_session()
        model = NativeRoutingModel()
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(
            set(model.offered_tools), {"read_file", "edit_file", "save_observation_draft", "finish_analysis", "stop_analysis", "crop", "region_stats"}
        )
        images = [
            block
            for message in model.requests[0]
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "image_url"
        ]
        self.assertEqual([block["image_url"]["url"] for block in images], [session.reference_url])
        self.assertEqual(context(model.requests[0])["scope"], "whole_scene")
        self.assertEqual([item.id for item in session.observations.features], ["F8", "F11"])

    def test_measurements_inject_real_crop_and_preserve_only_unique_in_bounds_anchors(self) -> None:
        session = self.make_session()
        region = {"left": 0, "top": 0, "right": 2, "bottom": 2}
        args = {"region": region, "question": "核对颜色"}
        model = NativeRoutingModel(
            responses=[
                call("crop", args, 1),
                call("region_stats", args, 2),
                call("region_stats", {**args, "question": "再次核对"}, 3),
                call("region_stats", {**args, "region": {**region, "right": 6}}, 4),
            ]
        )
        self.invoke(model, session)
        result = session.finalize()
        self.assertEqual(result.status, "completed")
        self.assertEqual([item.kind for item in result.reference_measurements], ["crop", "region_stats"])
        self.assertEqual(result.reference_measurements[1].mean_rgb, (240.0, 80.0, 140.0))
        images = [
            block
            for message in model.requests[1]
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "image_url"
        ]
        self.assertEqual(len(images), 2)
        data = base64.b64decode(images[1]["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(data)) as crop:
            self.assertEqual(crop.size, (2, 2))
            self.assertEqual(crop.getpixel((0, 0)), (240, 80, 140, 255))
        receipts = {message.tool_call_id: message for message in model.requests[4] if isinstance(message, ToolMessage)}
        self.assertTrue(json.loads(str(receipts["call-3"].content))["cached"])
        self.assertEqual(receipts["call-4"].status, "error")

    def test_reference_errors_are_repaired_locally_in_the_same_graph(self) -> None:
        session = self.make_session()
        invalid = observation()
        invalid["relations"] = [{"id": "R1", "participants": ["E1", "E99"], "description": "不存在的元素"}]
        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": invalid}),
                call("edit_file", {"file_path": "/draft/observations.json", "old_string": '"E99"', "new_string": '"E3"'}, 2),
            ]
        )
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.observations.relations[0].participants, ("E1", "E3"))
        self.assertEqual(len(list((session.directory / "draft/history").glob("*.json"))), 1)

    def test_duplicate_id_repair_keeps_original_relations(self) -> None:
        session = self.make_session()
        payload = observation()
        payload["elements"][1]["features"][0]["id"] = "F8"
        payload["relations"][0]["participants"] = ["E1", "E3-F8"]
        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": payload}),
                call("read_file", {"file_path": "/draft/observations.json"}, 2),
                call(
                    "edit_file",
                    {
                        "file_path": "/draft/observations.json",
                        "old_string": '"id": "F8",\n          "description": "浅灰圆形边界"',
                        "new_string": '"id": "F11",\n          "description": "浅灰圆形边界"',
                    },
                    3,
                ),
                call("edit_file", {"file_path": "/draft/observations.json", "old_string": '"E3-F8"', "new_string": '"F11"'}, 4),
                call("read_file", {"file_path": "/draft/observations.json"}, 5),
                call("finish_analysis", {}, 6),
            ]
        )
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        relation = session.observations.relations[0]
        self.assertEqual(relation.description, payload["relations"][0]["description"])
        self.assertEqual(relation.participants, ("E1", "F11"))

    def test_read_result_removed_before_actual_request_cannot_mark_seen(self) -> None:
        session = self.make_session()
        original = FixedInput._prepare

        def filtered(middleware: FixedInput, request: ModelRequest) -> ModelRequest:
            prepared = original(middleware, request)
            return prepared.override(messages=[m for m in prepared.messages if not isinstance(m, ToolMessage)])

        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": observation()}),
                call("read_file", {"file_path": "/draft/observations.json"}, 2),
                call("finish_analysis", {}, 3),
                call("stop_analysis", {"reason": "missing request body"}, 4),
            ]
        )
        with patch.object(FixedInput, "_prepare", filtered):
            self.invoke(model, session)
        self.assertIsNone(session.seen_revision)
        self.assertEqual(session.finalize().status, "failed")

    def test_finish_requires_actual_complete_latest_read(self) -> None:
        session = self.make_session(max_model_calls=4)
        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": observation()}),
                call("read_file", {"file_path": "/draft/observations.json", "limit": 1}, 2),
                call("finish_analysis", {}, 3),
                call("stop_analysis", {"reason": "partial read"}, 4),
            ]
        )
        result = self.invoke(model, session)
        errors = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "finish_analysis"]
        self.assertEqual(errors[0].status, "error")
        self.assertEqual(json.loads(errors[0].content)["code"], "draft_unseen")
        self.assertEqual(session.finalize().status, "failed")
        self.assertTrue(session.draft.path.exists())

    def test_edit_invalidates_old_read_and_new_full_read_allows_finish(self) -> None:
        session = self.make_session()
        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": observation()}),
                call("read_file", {"file_path": "/draft/observations.json"}, 2),
                call("edit_file", {"file_path": "/draft/observations.json", "old_string": "青蓝渐变", "new_string": "青绿到蓝渐变"}, 3),
                call("finish_analysis", {}, 4),
                call("read_file", {"file_path": "/draft/observations.json"}, 5),
                call("finish_analysis", {}, 6),
            ]
        )
        result = self.invoke(model, session)
        finishes = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "finish_analysis"]
        self.assertEqual([m.status for m in finishes], ["error", "success"])
        self.assertEqual(session.observations.features[0].description, "青绿到蓝渐变")
        self.assertFalse(any(path.startswith("/draft/") for path in result["files"]))

    def test_save_and_read_batch_runs_in_order(self) -> None:
        session = self.make_session()
        conflict = AIMessage(
            content="",
            tool_calls=[
                {"name": "save_observation_draft", "args": {"report": observation()}, "id": "submit"},
                {"name": "read_file", "args": {"file_path": "/skills/scene-observer/references/grouping.md"}, "id": "read"},
            ],
        )
        result = self.invoke(NativeRoutingModel(responses=[conflict]), session)
        rejected = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.tool_call_id in {"submit", "read"}]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(message.status == "success" for message in rejected))
        self.assertTrue(session.draft.path.exists())
        self.assertEqual(session.tool_refusals, 0)
        self.assertEqual(session.finalize().status, "completed")

    def test_paginated_draft_read_can_complete(self) -> None:
        session = self.make_session()
        report = observation()
        report["elements"][0]["features"].extend({"id": f"F{number}", "description": "额外可见纹理"} for number in range(100, 130))
        model = NativeRoutingModel(
            responses=[
                call("save_observation_draft", {"report": report}),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"file_path": "/draft/observations.json", "limit": 100}, "id": "page1"},
                        {"name": "read_file", "args": {"file_path": "/draft/observations.json", "offset": 100}, "id": "page2"},
                    ],
                ),
            ]
        )
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.seen_revision, session.draft.revision)
        self.assertIn("next offset 100", str(model.requests[-1]))

    def test_dependent_edits_and_read_batch_preserve_order(self) -> None:
        session = self.make_session()
        batch = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/observations.json", "old_string": "青蓝渐变", "new_string": "青绿渐变"},
                    "id": "edit1",
                },
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/observations.json", "old_string": "青绿渐变", "new_string": "柔和渐变"},
                    "id": "edit2",
                },
                {"name": "read_file", "args": {"file_path": "/draft/observations.json"}, "id": "read"},
            ],
        )
        result = self.invoke(NativeRoutingModel(responses=[call("save_observation_draft", {"report": observation()}), batch]), session)
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.observations.features[0].description, "柔和渐变")
        messages = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id in {"edit1", "edit2", "read"}]
        self.assertTrue(all(m.status == "success" for m in messages))
        self.assertEqual(session.tool_refusals, 0)

    def test_read_and_finish_batch_requires_next_model_response(self) -> None:
        session = self.make_session()
        batch = AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "args": {"file_path": "/draft/observations.json"}, "id": "read"},
                {"name": "finish_analysis", "args": {}, "id": "finish"},
            ],
        )
        result = self.invoke(NativeRoutingModel(responses=[call("save_observation_draft", {"report": observation()}), batch]), session)
        finishes = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "finish_analysis"]
        self.assertEqual([m.status for m in finishes], ["error", "success"])
        self.assertIn("下一次模型响应", str(finishes[0].content))
        self.assertEqual(session.finalize().status, "completed")

    def test_guessed_delegation_write_and_host_reads_are_rejected(self) -> None:
        session = self.make_session()
        probes = [
            call("task", {"subagent_type": "general-purpose", "description": "delegate"}),
            call("write_file", {"file_path": "/skills/scene-observer/SKILL.md", "content": "changed"}, 2),
            call("read_file", {"file_path": "/etc/passwd"}, 3),
        ]
        result = self.invoke(NativeRoutingModel(responses=probes), session)
        failures = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
        self.assertEqual(len(failures), 3)
        self.assertNotIn("changed", str(result["files"]["/skills/scene-observer/SKILL.md"]))

    def test_resources_are_disclosed_on_read_and_sdk_history_remains_readable(self) -> None:
        session = self.make_session()
        initial = analysis_input(session)
        initial["files"]["/conversation_history/history.md"] = create_file_data("本轮外置历史内容")
        paths = [f"/skills/scene-observer/references/{name}.md" for name in ("grouping", "relations", "uncertainty")]
        contents = ["\n".join(initial["files"][path]["content"]) for path in paths]
        model = NativeRoutingModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": path}, "id": path} for path in paths]),
                call("read_file", {"file_path": "/conversation_history/history.md"}, 2),
            ]
        )
        result = self.invoke(model, session, initial)
        reads = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.name == "read_file"]
        self.assertEqual(len(reads), 5)
        self.assertTrue(all(message.status == "success" for message in reads))
        for content in contents:
            self.assertNotIn(content, str(model.requests[0]))
            self.assertIn(content.splitlines()[-1], str(model.requests[1]))
        self.assertIn("本轮外置历史内容", str(reads[3].content))
        self.assertCountEqual(session.execution()["resource_read_results"], [{"path": path, "status": "success"} for path in paths])
        self.assertEqual(session.finalize().status, "completed")

    def test_failed_skill_read_is_not_recorded_as_success(self) -> None:
        session = self.make_session()
        initial = analysis_input(session)
        path = "/skills/scene-observer/references/relations.md"
        del initial["files"][path]
        self.invoke(NativeRoutingModel(responses=[call("read_file", {"file_path": path})]), session, initial)
        self.assertEqual(session.execution()["resource_read_results"], [{"path": path, "status": "error"}])

    def test_plain_text_and_budget_exhaustion_do_not_create_reports(self) -> None:
        session = self.make_session()
        self.invoke(NativeRoutingModel(responses=[AIMessage(content="已完成")]), session)
        self.assertEqual(session.finalize().status, "failed")
        limited = self.make_session(max_model_calls=1)
        model = NativeRoutingModel(responses=[call("read_file", {"file_path": "/skills/scene-observer/references/grouping.md"})])
        self.invoke(model, limited)
        self.assertEqual(limited.finalize().status, "failed")
        self.assertEqual(limited.reason, "model_call_limit")
        self.assertEqual(len(model.requests), 1)

    def test_sdk_summary_actually_externalizes_history_and_the_agent_can_read_it(self) -> None:
        session = self.make_session()
        initial = analysis_input(session)
        initial["messages"] = [
            message for number in range(25) for message in (HumanMessage(content=f"历史观察 {number}"), AIMessage(content="已记录"))
        ] + initial["messages"]
        model = NativeRoutingModel(responses=[call("read_file", {"file_path": "/conversation_history/offline-history.md"})])
        with (
            patch.object(SummarizationMiddleware, "_should_summarize", side_effect=[True] + [False] * 10),
            patch.object(SummarizationMiddleware, "_over_budget", return_value=False),
            patch.object(SummarizationMiddleware, "_determine_cutoff_index", side_effect=lambda messages: len(messages) - 1),
            patch.object(SummarizationMiddleware, "_get_session_id", return_value="offline-history"),
            patch.object(SummarizationMiddleware, "_create_summary", return_value="本轮历史已外置") as summary,
        ):
            result = self.invoke(model, session, initial)
        summary.assert_called_once()
        self.assertIn("/conversation_history/offline-history.md", result["files"])
        reads = [str(message.content) for message in result["messages"] if isinstance(message, ToolMessage) and message.name == "read_file"]
        self.assertIn("历史观察 0", reads[0])
        self.assertEqual(session.finalize().status, "completed")

    def test_stop_is_terminal_and_snapshot_io_failure_cannot_be_accepted(self) -> None:
        session = self.make_session()
        model = NativeRoutingModel(responses=[call("stop_analysis", {"reason": "无法辨认主体"})])
        self.invoke(model, session)
        self.assertEqual(session.finalize().status, "failed")
        self.assertEqual(len(model.requests), 1)
        failing = self.make_session()
        with patch("shader_deep.workflows.analysis.session.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            failing._submit(SceneObservationSubmission.model_validate(observation()))
        self.assertIsNone(failing.submission)
        self.assertEqual(failing.finalize().status, "failed")

    def test_accepted_state_skips_even_a_pending_sdk_summary(self) -> None:
        session = self.make_session()
        model = NativeRoutingModel()
        initial = analysis_input(session)
        initial["messages"] = [HumanMessage(content="旧的逐项观察" * 600), AIMessage(content="历史记录")] * 20 + initial["messages"]
        with (
            patch.object(SummarizationMiddleware, "_should_summarize", side_effect=lambda *_: session.finished),
            patch.object(SummarizationMiddleware, "_over_budget", return_value=False),
            patch.object(SummarizationMiddleware, "_create_summary", side_effect=AssertionError("终态后不应摘要")) as summary,
        ):
            self.invoke(model, session, initial)
        summary.assert_not_called()
        self.assertEqual(len(model.requests), 3)


class AsyncNativeAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_edits_then_read_are_ordered(self) -> None:
        helper = NativeAnalysisTests()
        self.addCleanup(helper.doCleanups)
        session = helper.make_session()
        batch = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/observations.json", "old_string": "青蓝渐变", "new_string": "青绿渐变"},
                    "id": "edit1",
                },
                {
                    "name": "edit_file",
                    "args": {"file_path": "/draft/observations.json", "old_string": "青绿渐变", "new_string": "柔和渐变"},
                    "id": "edit2",
                },
                {"name": "read_file", "args": {"file_path": "/draft/observations.json"}, "id": "read"},
            ],
        )
        model = NativeRoutingModel(responses=[call("save_observation_draft", {"report": observation()}), batch])
        await build_analysis_agent(model, session, session.options).ainvoke(analysis_input(session))
        self.assertEqual(session.finalize().status, "completed")
        self.assertEqual(session.observations.features[0].description, "柔和渐变")

    async def test_async_stop_prevents_later_batch_mutation(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buffer, format="PNG")
        session = AnalysisSession(
            "观察", "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(), directory, AnalysisOptions(max_request_retries=0)
        )
        conflict = AIMessage(
            content="",
            tool_calls=[
                {"name": "stop_analysis", "args": {"reason": "stop"}, "id": "stop"},
                {"name": "save_observation_draft", "args": {"report": observation()}, "id": "submit"},
            ],
        )
        model = NativeRoutingModel(responses=[conflict])
        initial = analysis_input(session)
        initial["messages"] = [HumanMessage(content="旧的逐项观察" * 600), AIMessage(content="历史记录")] * 20 + initial["messages"]
        with (
            patch.object(SummarizationMiddleware, "_should_summarize", side_effect=lambda *_: session.finished),
            patch.object(SummarizationMiddleware, "_over_budget", return_value=False),
            patch.object(SummarizationMiddleware, "_acreate_summary", side_effect=AssertionError("终态后不应摘要")) as summary,
        ):
            result = await build_analysis_agent(model, session, session.options).ainvoke(initial)
        summary.assert_not_called()
        failures = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(session.finalize().status, "failed")
        self.assertFalse(session.draft.path.exists())
