"""使用真实 Chromium WebGL2 执行 Shader, 独立解码 PNG 验证像素."""

from __future__ import annotations

from io import BytesIO
from typing import ClassVar
from unittest import TestCase

from PIL import Image

from shader_deep.rendering import RenderError, WebGL2Renderer

RED = "void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0, 0.0, 0.0, 1.0); }"


class WebGL2RenderTests(TestCase):
    renderer: ClassVar[WebGL2Renderer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.renderer = WebGL2Renderer()
        cls.renderer.__enter__()
        cls.addClassCleanup(cls.renderer.close)

    def image(self, code: str, width: int = 32, height: int = 24, time: float = 0.0) -> Image.Image:
        png = self.renderer.render(code, width, height, time)
        with Image.open(BytesIO(png)) as decoded:
            self.assertEqual(decoded.format, "PNG")
            image = decoded.convert("RGBA")
        self.addCleanup(image.close)
        return image

    def test_solid_color_and_exact_dimensions(self) -> None:
        image = self.image(RED, 73, 41)
        self.assertEqual(image.size, (73, 41))
        self.assertEqual(image.getextrema(), ((255, 255), (0, 0), (0, 0), (255, 255)))

    def test_gradient_has_correct_orientation_and_resolution(self) -> None:
        code = "void mainImage(out vec4 c, in vec2 p) { c = vec4(p / iResolution.xy, 0.0, 1.0); }"
        image = self.image(code, 80, 32)
        self.assertLess(image.getpixel((0, 0))[0], 5)
        self.assertGreater(image.getpixel((79, 0))[0], 250)
        self.assertGreater(image.getpixel((0, 0))[1], 245)
        self.assertLess(image.getpixel((0, 31))[1], 10)

    def test_time_is_explicit_and_repeated_time_is_stable(self) -> None:
        code = "void mainImage(out vec4 c, in vec2 p) { c = vec4(iTime / 2.0, 0.0, 0.0, 1.0); }"
        start = self.image(code, time=0.0)
        middle = self.image(code, time=1.0)
        end = self.image(code, time=2.0)
        repeated = self.image(code, time=1.0)
        self.assertEqual(start.getpixel((0, 0))[0], 0)
        self.assertAlmostEqual(middle.getpixel((0, 0))[0], 128, delta=1)
        self.assertEqual(end.getpixel((0, 0))[0], 255)
        self.assertEqual(middle.tobytes(), repeated.tobytes())

    def test_shader_alpha_is_preserved(self) -> None:
        code = "void mainImage(out vec4 c, in vec2 p) { c = vec4(0.0, 1.0, 0.0, 0.5); }"
        pixel = self.image(code).getpixel((4, 4))
        self.assertEqual(pixel[:3], (0, 255, 0))
        self.assertAlmostEqual(pixel[3], 128, delta=1)

    def test_compile_error_returns_driver_log_and_next_render_succeeds(self) -> None:
        code = "void mainImage(out vec4 c, in vec2 p) { c = vec4(unknown_symbol); }"
        with self.assertRaises(RenderError) as caught:
            self.renderer.render(code, 32, 24, 0.0)
        self.assertEqual(caught.exception.stage, "fragment_compile")
        self.assertIn("unknown_symbol", caught.exception.log)
        self.assertEqual(self.image(RED).getpixel((5, 5)), (255, 0, 0, 255))

    def test_link_error_and_next_render_succeeds(self) -> None:
        code = "in vec2 not_written; void mainImage(out vec4 c, in vec2 p) { c = vec4(not_written, 0.0, 1.0); }"
        with self.assertRaises(RenderError) as caught:
            self.renderer.render(code, 32, 24, 0.0)
        self.assertEqual(caught.exception.stage, "link")
        self.assertTrue(caught.exception.log)
        self.assertEqual(self.image(RED).getpixel((5, 5)), (255, 0, 0, 255))

    def test_discard_does_not_retain_pixels_from_previous_render(self) -> None:
        self.image(RED)
        code = "void mainImage(out vec4 c, in vec2 p) { if (p.x < iResolution.x / 2.0) discard; c = vec4(0.0, 1.0, 0.0, 1.0); }"
        image = self.image(code)
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(image.getpixel((31, 0)), (0, 255, 0, 255))

    def test_serial_renders_can_change_dimensions(self) -> None:
        for size in ((31, 67), (128, 16), (64, 64)):
            with self.subTest(size=size):
                image = self.image(RED, *size)
                self.assertEqual(image.size, size)
                self.assertEqual(image.getpixel((size[0] - 1, size[1] - 1)), (255, 0, 0, 255))

    def test_oversized_buffer_is_rejected_without_resizing_silently(self) -> None:
        with self.assertRaises(RenderError) as caught:
            self.renderer.render(RED, 2**31, 16, 0.0)
        self.assertEqual(caught.exception.stage, "size")
        self.assertEqual(self.image(RED, 7, 9).size, (7, 9))


class RendererLifecycleTests(TestCase):
    def test_context_manager_closes_and_can_be_reopened(self) -> None:
        renderer = WebGL2Renderer()
        for _ in range(2):
            with renderer:
                png = renderer.render(RED, 3, 5, 0.0)
                with Image.open(BytesIO(png)) as image:
                    self.assertEqual(image.size, (3, 5))
            with self.assertRaises(RenderError) as caught:
                renderer.render(RED, 3, 5, 0.0)
            self.assertEqual(caught.exception.stage, "init")

    def test_escaping_compile_error_closes_renderer(self) -> None:
        renderer = WebGL2Renderer()
        with self.assertRaises(RenderError) as caught, renderer:
            renderer.render("invalid_glsl", 8, 8, 0.0)
        self.assertEqual(caught.exception.stage, "fragment_compile")
        with renderer:
            png = renderer.render(RED, 8, 8, 0.0)
            with Image.open(BytesIO(png)) as image:
                self.assertEqual(image.size, (8, 8))
