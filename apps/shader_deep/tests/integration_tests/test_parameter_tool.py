"""两个工具共享常驻会话, 通过 PNG 像素验证局部更新和取图."""

import base64
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_run import run_parameter_task
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.experiments.tools import build_parameter_tools
from shader_deep.rendering import RenderError, ShaderPreview, WebGL2Renderer

SHADER = "uniform float RED; uniform float GREEN; void mainImage(out vec4 c, in vec2 p) { c = vec4(RED, GREEN, 0.0, 1.0); }"


class ParameterToolRenderTests(TestCase):
    def test_closed_browser_stops_agent_and_records_unexpected_disconnect(self) -> None:
        specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}}
        model = FakeMessagesListChatModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "capture_preview", "args": {}, "id": "capture"}]),
                AIMessage(content="should not run"),
            ]
        )
        with ShaderPreview(SHADER, parameters=specs, width=7, height=5, headless=True) as preview:
            preview.set_parameters({"RED": 0.5})
            with patch.object(FakeMessagesListChatModel, "bind_tools", return_value=model):
                agent = build_parameter_agent(preview=preview, model=model)

                # 故障注入只关闭测试自己的 Chromium, 必须在所属工作线程操作.
                def close_test_browser() -> None:
                    preview._browser._page.context.browser.close()

                with self.assertLogs("shader_deep.rendering._preview_browser", level="WARNING") as logs:
                    preview._run(close_test_browser)
                with self.assertRaises(RenderError):
                    run_parameter_task(agent, HumanMessage(content="inspect current image"), preview)
            self.assertEqual(model.i, 1)
            self.assertTrue(any("browser_disconnected" in line and "owner_closing=False" in line for line in logs.output))

    def test_comparison_and_history_preserve_real_canvas_pixels(self) -> None:
        specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}, "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.25}}
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "reference.png"
            with WebGL2Renderer() as renderer:
                target.write_bytes(renderer.render(SHADER, 100, 5, 0.0, uniforms={"RED": 0.8, "GREEN": 0.25}))
            with ShaderPreview(SHADER, parameters=specs, width=100, height=5, headless=True) as preview:
                tools = {operation.name: operation for operation in build_parameter_tools(preview, reference_path=target)}
                blocks = tools["compare_preview"].invoke({})
                comparison_png = base64.b64decode(blocks[1]["image_url"]["url"].split(",", 1)[1])
                with Image.open(BytesIO(comparison_png)) as comparison:
                    self.assertEqual(comparison.size, (200, 29))
                    self.assertEqual(comparison.getpixel((0, 24)), (204, 64, 0, 255))
                    self.assertEqual(comparison.getpixel((100, 24)), (0, 64, 0, 255))
                tools["set_parameters"].invoke({"parameters": {"RED": 0.8}})
                tools["compare_preview"].invoke({})
                history = tools["get_trial_history"].invoke({"trial_ids": [1]})
                self.assertEqual(json.loads(history[1]["text"])["parameters"], {"RED": 0.0, "GREEN": 0.25})
                with Image.open(BytesIO(base64.b64decode(history[2]["image_url"]["url"].split(",", 1)[1]))) as image:
                    self.assertEqual(image.getpixel((0, 0)), (0, 64, 0, 255))
                self.assertEqual(dict(preview.capture().parameters), {"RED": 0.8, "GREEN": 0.25})

    def test_generated_target_and_current_capture_are_distinct_images(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "reference.png"
            with WebGL2Renderer() as renderer:
                target_png = renderer.render(SHADER, 7, 5, 0.0, uniforms={"RED": 0.8, "GREEN": 0.25})
            target.write_bytes(target_png)
            task = build_reference_task(target)
            self.assertEqual(base64.b64decode(task.content[1]["image_url"]["url"].split(",", 1)[1]), target_png)
            specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}, "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.25}}
            with ShaderPreview(SHADER, parameters=specs, width=7, height=5, headless=True) as preview:
                _, capture = build_parameter_tools(preview)
                current = capture.invoke({})
                current_png = base64.b64decode(current[1]["image_url"]["url"].split(",", 1)[1])
                self.assertNotEqual(current_png, target_png)
                with Image.open(BytesIO(target_png)) as image:
                    self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (204, 64, 0, 255))
                with Image.open(BytesIO(current_png)) as image:
                    self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (0, 64, 0, 255))

    def test_two_tools_share_preview_and_return_current_pixels(self) -> None:
        specs = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}, "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.25}}
        with ShaderPreview(SHADER, parameters=specs, width=7, height=5, headless=True) as preview:
            update, capture = build_parameter_tools(preview)
            for parameters, pixel in (({"RED": 0.0, "GREEN": 0.5}, (0, 128, 0, 255)), ({"RED": 1.0}, (255, 128, 0, 255))):
                update.invoke({"parameters": parameters})
                message = capture.invoke({"type": "tool_call", "name": "capture_preview", "id": "image", "args": {}})
                record = json.loads(message.content[0]["text"])
                self.assertEqual(record["parameters"]["GREEN"], 0.5)
                png = base64.b64decode(message.content[1]["image_url"]["url"].split(",", 1)[1])
                with Image.open(BytesIO(png)) as image:
                    self.assertEqual(image.size, (7, 5))
                    self.assertEqual(image.convert("RGBA").getpixel((0, 0)), pixel)
            rejected = update.invoke({"type": "tool_call", "name": "set_parameters", "id": "bad", "args": {"parameters": {"RED": 9.0}}})
            self.assertEqual(rejected.status, "error")
            self.assertEqual(dict(preview.capture().parameters), {"RED": 1.0, "GREEN": 0.5})
        with self.assertRaises(RenderError):
            capture.invoke({"type": "tool_call", "name": "capture_preview", "id": "closed", "args": {}})
