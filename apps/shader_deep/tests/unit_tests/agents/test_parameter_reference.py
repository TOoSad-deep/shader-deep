"""参考图以真实字节进入模型输入, 并与当前渲染图片同时保留."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import Mock, patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.rendering import PreviewSnapshot, ShaderPreview

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


class InputRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.requests: list[list[BaseMessage]] = []

    def on_chat_model_start(self, serialized: dict[str, object], messages: list[list[BaseMessage]], **kwargs: object) -> None:
        # 保留框架回调签名, 这里只采集消息, 不保存模型配置及回调控制字段.
        del serialized, kwargs
        self.requests.append(messages[0])


class ReferenceTaskTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "hidden-target-0.8.png"
        with Image.new("RGB", (2, 2), (128, 0, 0)) as image:
            image.save(self.reference)
        self.reference_bytes = self.reference.read_bytes()

    def test_message_freezes_original_image_without_leaking_path(self) -> None:
        message = build_reference_task(self.reference)
        self.reference.unlink()
        image_url = message.content[1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), self.reference_bytes)
        self.assertNotIn(self.reference.name, str(message.content))
        self.assertNotIn(str(self.root), str(message.content))

    def test_missing_file_and_non_png_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_reference_task(self.root / "missing.png")
        other = self.root / "reference.jpg"
        other.write_bytes(self.reference_bytes)
        with self.assertRaises(ValueError):
            build_reference_task(other)

    def test_reference_and_current_preview_reach_later_model_inputs(self) -> None:
        with BytesIO() as buffer:
            with Image.new("RGB", (2, 2), (0, 0, 0)) as image:
                image.save(buffer, format="PNG")
            current_png = buffer.getvalue()
        preview = Mock(spec=ShaderPreview)
        preview.parameter_specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
        preview.capture.return_value = PreviewSnapshot(png=current_png, parameters={"RED": 0.0}, revision=0, time=0.0, width=2, height=2)
        model = FakeMessagesListChatModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "capture_preview", "args": {}, "id": "baseline"}]),
                AIMessage(content="完成观察"),
            ]
        )
        recorder = InputRecorder()
        with patch.object(FakeMessagesListChatModel, "bind_tools", return_value=model):
            agent = build_parameter_agent(preview=preview, model=model)
            agent.invoke({"messages": [build_reference_task(self.reference)]}, config={"callbacks": [recorder], "recursion_limit": 12})
        for request in recorder.requests:
            target = next(message for message in request if isinstance(message, HumanMessage))
            self.assertEqual(base64.b64decode(target.content[1]["image_url"]["url"].split(",", 1)[1]), self.reference_bytes)
        capture = next(message for message in recorder.requests[-1] if isinstance(message, ToolMessage))
        self.assertEqual(base64.b64decode(capture.content[1]["image_url"]["url"].split(",", 1)[1]), current_png)
        self.assertNotEqual(current_png, self.reference_bytes)
