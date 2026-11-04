"""通过公开 Python 接口验证持续预览、参数快照、线程和错误清理."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest import TestCase

from PIL import Image

from shader_deep.rendering import RenderError, ShaderPreview

SHADER = "uniform float RED; uniform float GREEN; void mainImage(out vec4 c, in vec2 p) { c = vec4(RED, GREEN, 0.0, 1.0); }"


class PythonPreviewTests(TestCase):
    def test_threaded_updates_preserve_state_and_return_independent_snapshots(self) -> None:
        parameters = {"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}, "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.25}}
        preview = ShaderPreview(SHADER, parameters=parameters, width=7, height=5, time=0.5, headless=True)
        parameters["RED"]["max"] = 0.0
        with preview:
            before = preview.capture()
            with ThreadPoolExecutor(max_workers=2) as callers:
                red = callers.submit(preview.set_parameters, {"RED": 1.0})
                green = callers.submit(preview.set_parameters, {"GREEN": 0.5})
                red.result()
                green.result()
                after = callers.submit(preview.capture).result()
            self.assertEqual(dict(after.parameters), {"RED": 1.0, "GREEN": 0.5})
            self.assertEqual(after.revision, 2)
            self.assertEqual((after.width, after.height, after.time), (7, 5, 0.5))
            self.assertEqual(dict(before.parameters), {"RED": 0.0, "GREEN": 0.25})
            with Image.open(BytesIO(after.png)) as image:
                self.assertEqual(image.size, (7, 5))
                self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (255, 128, 0, 255))
            with self.assertRaises(RenderError) as caught:
                preview.set_parameters({"RED": 0.5, "GREEN": 9.0})
            self.assertEqual(caught.exception.stage, "uniform")
            self.assertEqual(preview.capture(), after)
        preview.close()
        with self.assertRaises(RenderError):
            preview.capture()

    def test_failed_initialization_preserves_compile_log_and_releases_session(self) -> None:
        preview = ShaderPreview("invalid_glsl", parameters={}, headless=True)
        with self.assertRaises(RenderError) as caught, preview:
            self.fail("Invalid shader was accepted")
        self.assertEqual(caught.exception.stage, "fragment_compile")
        self.assertTrue(caught.exception.log)
        preview.close()
        with self.assertRaises(RenderError):
            preview.capture()

    def test_animation_snapshot_records_time_and_exception_exit_closes(self) -> None:
        preview = ShaderPreview(SHADER, parameters={}, time=None, headless=True)
        with self.assertRaisesRegex(RuntimeError, "caller failure"), preview:
            first = preview.capture()
            second = preview.capture()
            self.assertGreaterEqual(first.time, 0.0)
            self.assertGreaterEqual(second.time, first.time)
            msg = "caller failure"
            raise RuntimeError(msg)
        with self.assertRaises(RenderError):
            preview.capture()
