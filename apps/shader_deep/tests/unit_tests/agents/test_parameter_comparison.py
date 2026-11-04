"""验证模型收到的并排像素、历史快照和消息内容, 不依赖浏览器或外部模型."""

import base64
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.experiments.tools import build_parameter_tools
from shader_deep.rendering import PreviewSnapshot, RenderError, ShaderPreview
from tests.unit_tests.agents.test_parameter_reference import InputRecorder


def png(color: tuple[int, int, int, int], size: tuple[int, int] = (100, 3)) -> bytes:
    with Image.new("RGBA", size, color) as image, BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


def image_bytes(block: dict) -> bytes:
    return base64.b64decode(block["image_url"]["url"].split(",", 1)[1])


class ParameterComparisonTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.reference = Path(temporary.name) / "hidden-target-0.8.png"
        self.reference_png = png((204, 64, 0, 128))
        self.reference.write_bytes(self.reference_png)
        self.current_png = png((0, 64, 0, 255))
        self.preview = Mock(spec=ShaderPreview)
        self.preview.parameter_specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
        self.snapshot = PreviewSnapshot(png=self.current_png, parameters={"RED": 0.0}, revision=0, time=0.0, width=100, height=3)
        self.preview.capture.return_value = self.snapshot
        self.preview.set_parameters.return_value = {"RED": 0.5}
        self.tools = {operation.name: operation for operation in build_parameter_tools(self.preview, reference_path=self.reference)}

    def test_comparison_preserves_both_pixel_regions_and_freezes_reference(self) -> None:
        self.reference.unlink()
        blocks = self.tools["compare_preview"].invoke({})
        metadata = json.loads(blocks[0]["text"])
        self.assertEqual(metadata["parameters"], {"RED": 0.0})
        self.assertEqual(metadata["trial_id"], 1)
        self.assertEqual(metadata["layout"], "reference_left_current_right")
        self.assertEqual((metadata["image_width"], metadata["image_height"]), (200, 27))
        with Image.open(BytesIO(image_bytes(blocks[1]))) as comparison:
            self.assertEqual(comparison.size, (200, 27))
            with Image.open(BytesIO(self.reference_png)) as reference, Image.open(BytesIO(self.current_png)) as current:
                self.assertEqual(comparison.crop((0, 24, 100, 27)).tobytes(), reference.tobytes())
                self.assertEqual(comparison.crop((100, 24, 200, 27)).tobytes(), current.tobytes())

    def test_history_replays_original_snapshot_without_recapture_or_state_change(self) -> None:
        self.tools["compare_preview"].invoke({})
        self.tools["set_parameters"].invoke({"parameters": {"RED": 0.5}})
        second_png = png((128, 64, 0, 255))
        self.preview.capture.return_value = PreviewSnapshot(png=second_png, parameters={"RED": 0.5}, revision=1, time=0.0, width=100, height=3)
        self.tools["capture_preview"].invoke({})
        self.preview.reset_mock()
        blocks = self.tools["get_trial_history"].invoke({"trial_ids": [2, 1]})
        self.assertEqual(json.loads(blocks[0]["text"]), {"total_trials": 2, "trial_ids": [2, 1]})
        self.assertEqual(json.loads(blocks[1]["text"])["parameters"], {"RED": 0.5})
        self.assertEqual(image_bytes(blocks[2]), second_png)
        self.assertEqual(json.loads(blocks[3]["text"])["parameters"], {"RED": 0.0})
        self.assertEqual(image_bytes(blocks[4]), self.current_png)
        self.preview.capture.assert_not_called()
        self.preview.set_parameters.assert_not_called()
        self.preview.close.assert_not_called()

    def test_history_defaults_to_recent_three_and_does_not_rank(self) -> None:
        empty = self.tools["get_trial_history"].invoke({})
        self.assertEqual(json.loads(empty[0]["text"]), {"total_trials": 0, "trial_ids": []})
        for _ in range(4):
            self.tools["compare_preview"].invoke({})
        blocks = self.tools["get_trial_history"].invoke({})
        self.assertEqual(json.loads(blocks[0]["text"]), {"total_trials": 4, "trial_ids": [2, 3, 4]})
        # 可见字段白名单约束评分、排名、路径等信息均不会混入历史.
        expected = {"trial_id", "parameters", "revision", "time", "width", "height"}
        for block in blocks[1::2]:
            self.assertEqual(set(json.loads(block["text"])), expected)

    def test_unknown_or_invalid_history_ids_are_error_tool_messages(self) -> None:
        self.tools["compare_preview"].invoke({})
        for ids in ([], [0], [-1], [2], [1, 1], [1, 2, 3, 4], [True], ["1"]):
            with self.subTest(ids=ids):
                message = self.tools["get_trial_history"].invoke(
                    {"type": "tool_call", "name": "get_trial_history", "id": "bad", "args": {"trial_ids": ids}}
                )
                self.assertEqual(message.status, "error")

    def test_failed_captures_and_mismatched_dimensions_do_not_create_trials(self) -> None:
        for failure in (RenderError("closed", stage="preview"), None):
            self.preview.capture.side_effect = failure
            self.preview.capture.return_value = PreviewSnapshot(
                png=png((0, 0, 0, 255), (1, 1)), parameters={}, revision=0, time=0.0, width=1, height=1
            )
            call = {"type": "tool_call", "name": "compare_preview", "id": "failed", "args": {}}
            if failure is not None:
                with self.assertRaises(RenderError):
                    self.tools["compare_preview"].invoke(call)
            else:
                self.assertEqual(self.tools["compare_preview"].invoke(call).status, "error")
        blocks = self.tools["get_trial_history"].invoke({})
        self.assertEqual(json.loads(blocks[0]["text"])["total_trials"], 0)

    def test_reference_must_be_valid_png_and_require_preview(self) -> None:
        self.reference.write_bytes(b"not a png")
        with self.assertRaises(ValueError):
            build_parameter_tools(self.preview, reference_path=self.reference)
        with self.assertRaises(ValueError):
            build_parameter_agent(reference_path=self.reference)

    def test_history_is_isolated_per_tool_binding(self) -> None:
        self.tools["compare_preview"].invoke({})
        other = {operation.name: operation for operation in build_parameter_tools(self.preview, reference_path=self.reference)}
        blocks = other["get_trial_history"].invoke({})
        self.assertEqual(json.loads(blocks[0]["text"])["total_trials"], 0)

    def test_native_agent_receives_comparison_and_history_images_without_scores(self) -> None:
        responses = [
            AIMessage(content="", tool_calls=[{"name": "compare_preview", "args": {}, "id": "baseline"}]),
            AIMessage(content="", tool_calls=[{"name": "set_parameters", "args": {"parameters": {"RED": 0.5}}, "id": "update"}]),
            AIMessage(content="", tool_calls=[{"name": "compare_preview", "args": {}, "id": "after"}]),
            AIMessage(content="", tool_calls=[{"name": "get_trial_history", "args": {"trial_ids": [1]}, "id": "history"}]),
            AIMessage(content="仍有差异"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        recorder = InputRecorder()
        with patch.object(FakeMessagesListChatModel, "bind_tools", return_value=model):
            agent = build_parameter_agent(preview=self.preview, reference_path=self.reference, model=model)
            agent.invoke({"messages": [build_reference_task(self.reference)]}, config={"callbacks": [recorder], "recursion_limit": 20})
        messages = [message for message in recorder.requests[-1] if isinstance(message, ToolMessage)]
        self.assertEqual([message.name for message in messages], ["compare_preview", "set_parameters", "compare_preview", "get_trial_history"])
        self.assertTrue(all(message.status == "success" for message in messages))
        with Image.open(BytesIO(image_bytes(messages[0].content[1]))) as comparison:
            self.assertEqual(comparison.size, (200, 27))
        self.assertEqual(image_bytes(messages[-1].content[2]), self.current_png)
        for message in messages:
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    payload = json.loads(block["text"])
                    self.assertTrue(
                        set(payload)
                        <= {
                            "trial_id",
                            "parameters",
                            "revision",
                            "time",
                            "width",
                            "height",
                            "layout",
                            "image_width",
                            "image_height",
                            "total_trials",
                            "trial_ids",
                        }
                    )
        self.assertNotIn(self.reference.name, str(recorder.requests))
