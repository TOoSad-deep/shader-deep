"""工具只转发会话操作, 将真实快照内容转换为模型能读取的消息."""

import base64
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_tool import build_parameter_tools
from shader_deep.rendering import PreviewSnapshot, RenderError, ShaderPreview

PNG = b"captured-png"


class ParameterToolTests(TestCase):
    def setUp(self) -> None:
        self.preview = Mock(spec=ShaderPreview)
        self.preview.parameter_specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
        self.preview.set_parameters.return_value = {"RED": 0.8}
        self.preview.capture.return_value = PreviewSnapshot(png=PNG, parameters={"RED": 0.8}, revision=1, time=0.5, width=7, height=5)
        self.update, self.capture = build_parameter_tools(self.preview)

    def test_update_returns_state_and_capture_returns_image_with_matching_metadata(self) -> None:
        state = self.update.invoke({"parameters": {"RED": 0.8}})
        self.assertEqual(json.loads(state)["parameters"], {"RED": 0.8})
        message = self.capture.invoke({"type": "tool_call", "name": "capture_preview", "id": "capture-1", "args": {}})
        self.assertIsInstance(message, ToolMessage)
        metadata = json.loads(message.content[0]["text"])
        self.assertEqual(metadata, {"revision": 1, "parameters": {"RED": 0.8}, "time": 0.5, "width": 7, "height": 5})
        self.assertEqual(base64.b64decode(message.content[1]["image_url"]["url"].split(",", 1)[1]), PNG)

    def test_model_input_schema_rejects_boolean_and_numeric_string(self) -> None:
        for value in (True, "0.8"):
            message = self.update.invoke({"type": "tool_call", "name": "set_parameters", "id": "bad-input", "args": {"parameters": {"RED": value}}})
            self.assertEqual(message.status, "error")
        self.preview.set_parameters.assert_not_called()

    def test_fatal_session_errors_propagate_to_the_caller(self) -> None:
        for operation, args, method in (
            (self.update, {"parameters": {"RED": 2.0}}, self.preview.set_parameters),
            (self.capture, {}, self.preview.capture),
        ):
            with self.subTest(operation=operation.name):
                method.side_effect = RenderError("session failure", stage="preview")
                with self.assertRaisesRegex(RenderError, "preview: session failure"):
                    operation.invoke({"type": "tool_call", "name": operation.name, "id": "failed", "args": args})

    def test_uniform_errors_remain_correctable_by_the_model(self) -> None:
        self.preview.set_parameters.side_effect = RenderError("out of range", stage="uniform")
        message = self.update.invoke({"type": "tool_call", "name": "set_parameters", "id": "range", "args": {"parameters": {"RED": 2.0}}})
        self.assertEqual(message.status, "error")
        self.assertIn("out of range", message.content)

    def test_deep_agent_consumes_capture_messages_without_a_main_agent(self) -> None:
        responses = [
            AIMessage(content="", tool_calls=[{"name": "capture_preview", "args": {}, "id": "baseline"}]),
            AIMessage(content="", tool_calls=[{"name": "set_parameters", "args": {"parameters": {"RED": 0.8}}, "id": "update"}]),
            AIMessage(content="", tool_calls=[{"name": "capture_preview", "args": {}, "id": "after"}]),
            AIMessage(content="已查看调整后的预览"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with patch.object(FakeMessagesListChatModel, "bind_tools", return_value=model):
            agent = build_parameter_agent(preview=self.preview, model=model)
            result = agent.invoke({"messages": [{"role": "user", "content": "增加红色"}]}, config={"recursion_limit": 20})
        self.assertEqual(result["messages"][-1].content, "已查看调整后的预览")
        images = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.name == "capture_preview"]
        self.assertEqual(len(images), 2)
        self.assertEqual(base64.b64decode(images[-1].content[1]["image_url"]["url"].split(",", 1)[1]), PNG)
        self.preview.close.assert_not_called()

    def test_agent_rejects_conflicting_configuration(self) -> None:
        with self.assertRaises(ValueError):
            build_parameter_agent(preview=self.preview, render_tool=self.update)
