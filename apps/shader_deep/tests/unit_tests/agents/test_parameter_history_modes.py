"""验证历史并排只改变呈现, 保留快照、原尺度像素和只读行为."""

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
from shader_deep.rendering import PreviewSnapshot, ShaderPreview
from tests.unit_tests.agents.test_parameter_comparison import image_bytes, png
from tests.unit_tests.agents.test_parameter_reference import InputRecorder


class ParameterHistoryModeTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.reference = Path(temporary.name) / "reference.png"
        self.reference_png = png((24, 100, 180, 128), (256, 3))
        self.reference.write_bytes(self.reference_png)
        self.preview = Mock(spec=ShaderPreview)
        self.preview.parameter_specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
        self.tools = {
            operation.name: operation for operation in build_parameter_tools(self.preview, reference_path=self.reference, history_mode="paired")
        }

    def capture(self, revision: int, red: float, *, size: tuple[int, int] = (256, 3)) -> bytes:
        captured = png((round(red * 255), 0, 20, 32), size)
        self.preview.capture.return_value = PreviewSnapshot(
            png=captured, parameters={"RED": red, "UNCHANGED": 0.25}, revision=revision, time=1.5, width=size[0], height=size[1]
        )
        self.tools["capture_preview"].invoke({})
        return captured

    def test_paired_history_uses_frozen_reference_and_selected_snapshot_without_browser(self) -> None:
        first = self.capture(3, 0.25)
        second = self.capture(8, 0.75)
        self.reference.write_bytes(png((255, 255, 255, 255), (256, 3)))
        self.preview.reset_mock()
        blocks = self.tools["get_trial_history"].invoke({"trial_ids": [2, 1]})
        self.assertEqual(json.loads(blocks[0]["text"]), {"total_trials": 2, "trial_ids": [2, 1]})
        for index, (trial_id, revision, red, original) in enumerate(((2, 8, 0.75, second), (1, 3, 0.25, first))):
            with self.subTest(trial_id=trial_id):
                metadata = json.loads(blocks[1 + index * 2]["text"])
                self.assertEqual(metadata["trial_id"], trial_id)
                self.assertEqual(metadata["revision"], revision)
                self.assertEqual(metadata["parameters"], {"RED": red, "UNCHANGED": 0.25})
                self.assertEqual(metadata["layout"], "reference_left_history_right")
                self.assertEqual((metadata["width"], metadata["height"], metadata["time"]), (256, 3, 1.5))
                self.assert_pair_pixels(image_bytes(blocks[2 + index * 2]), original)
        self.assertEqual(self.preview.mock_calls, [])
        # 反复回读不登记新 trial, 不改变任何既有快照或呈现.
        self.assertEqual(blocks, self.tools["get_trial_history"].invoke({"trial_ids": [2, 1]}))

    def assert_pair_pixels(self, paired: bytes, original: bytes) -> None:
        with (
            Image.open(BytesIO(paired)) as comparison,
            Image.open(BytesIO(self.reference_png)) as reference,
            Image.open(BytesIO(original)) as historical,
        ):
            self.assertEqual(comparison.size, (512, 27))
            self.assertEqual(comparison.crop((0, 24, 256, 27)).tobytes(), reference.tobytes())
            self.assertEqual(comparison.crop((256, 24, 512, 27)).tobytes(), historical.tobytes())

    def test_recent_three_keeps_trial_order_and_does_not_send_scores(self) -> None:
        self.assertEqual(json.loads(self.tools["get_trial_history"].invoke({})[0]["text"]), {"total_trials": 0, "trial_ids": []})
        for revision in (0, 2, 5, 6):
            self.capture(revision, 0.5)
        blocks = self.tools["get_trial_history"].invoke({})
        self.assertEqual(json.loads(blocks[0]["text"]), {"total_trials": 4, "trial_ids": [2, 3, 4]})
        expected = {"trial_id", "revision", "parameters", "time", "width", "height", "layout", "image_width", "image_height"}
        self.assertEqual([json.loads(block["text"])["revision"] for block in blocks[1::2]], [2, 5, 6])
        for block in blocks[1::2]:
            self.assertEqual(set(json.loads(block["text"])), expected)

    def test_size_mismatch_returns_tool_error_without_resize_or_dropping_trial(self) -> None:
        self.capture(2, 0.5, size=(4, 3))
        call = {"type": "tool_call", "name": "get_trial_history", "id": "size-mismatch", "args": {"trial_ids": [1]}}
        self.assertEqual(self.tools["get_trial_history"].invoke(call).status, "error")
        self.assertEqual(self.tools["get_trial_history"].invoke(call).status, "error")
        self.capture(3, 0.75)
        valid = self.tools["get_trial_history"].invoke({"trial_ids": [2]})
        self.assertEqual(json.loads(valid[0]["text"]), {"total_trials": 2, "trial_ids": [2]})

    def test_invalid_trial_requests_remain_tool_errors(self) -> None:
        self.capture(2, 0.5)
        for trial_ids in ([], [True], ["1"], [0], [2], [1, 1], [1, 2, 3, 4]):
            with self.subTest(trial_ids=trial_ids):
                call = {"type": "tool_call", "name": "get_trial_history", "id": "bad-id", "args": {"trial_ids": trial_ids}}
                self.assertEqual(self.tools["get_trial_history"].invoke(call).status, "error")

    def test_explicit_original_matches_default_payload_and_retains_raw_png(self) -> None:
        modes = ({}, {"history_mode": "original"})
        outputs = []
        original = png((100, 2, 3, 4), (256, 3))
        self.preview.capture.return_value = PreviewSnapshot(png=original, parameters={"RED": 0.5}, revision=1, time=0.0, width=256, height=3)
        for options in modes:
            tools = {operation.name: operation for operation in build_parameter_tools(self.preview, reference_path=self.reference, **options)}
            tools["capture_preview"].invoke({})
            outputs.append(tools["get_trial_history"].invoke({}))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(image_bytes(outputs[0][2]), original)
        self.assertNotIn("layout", json.loads(outputs[0][1]["text"]))

    def test_invalid_mode_or_missing_reference_fails_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires reference_path"):
            build_parameter_tools(self.preview, history_mode="paired")
        with self.assertRaisesRegex(ValueError, "history_mode"):
            build_parameter_tools(self.preview, reference_path=self.reference, history_mode="unknown")
        with self.assertRaisesRegex(ValueError, "requires reference_path"):
            build_parameter_agent(preview=self.preview, history_mode="paired")
        with self.assertRaisesRegex(ValueError, "history_mode"):
            build_parameter_agent(history_mode="unknown")

    def test_paired_history_png_enters_subsequent_native_agent_request(self) -> None:
        original = self.capture(7, 0.75)
        responses = [
            AIMessage(content="", tool_calls=[{"name": "compare_preview", "args": {}, "id": "baseline"}]),
            AIMessage(content="", tool_calls=[{"name": "get_trial_history", "args": {"trial_ids": [1]}, "id": "history"}]),
            AIMessage(content="仍有差异"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        recorder = InputRecorder()
        with patch.object(FakeMessagesListChatModel, "bind_tools", return_value=model):
            agent = build_parameter_agent(preview=self.preview, reference_path=self.reference, model=model, history_mode="paired")
            agent.invoke({"messages": [build_reference_task(self.reference)]}, config={"callbacks": [recorder], "recursion_limit": 20})
        messages = [message for message in recorder.requests[-1] if isinstance(message, ToolMessage)]
        self.assertEqual([message.name for message in messages], ["compare_preview", "get_trial_history"])
        history = messages[-1]
        self.assertEqual(history.status, "success")
        metadata = json.loads(history.content[1]["text"])
        self.assertEqual(metadata["layout"], "reference_left_history_right")
        self.assertEqual((metadata["trial_id"], metadata["revision"]), (1, 7))
        self.assertEqual(metadata["parameters"], {"RED": 0.75, "UNCHANGED": 0.25})
        self.assert_pair_pixels(image_bytes(history.content[2]), original)
