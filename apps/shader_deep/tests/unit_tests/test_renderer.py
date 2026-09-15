"""渲染器输入检查不需要启动浏览器."""

from unittest import TestCase

from shader_deep.rendering import RenderError, WebGL2Renderer

SHADER = "void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }"


class RendererInputTests(TestCase):
    def test_invalid_dimensions_are_rejected(self) -> None:
        renderer = WebGL2Renderer()
        for width, height in ((0, 10), (-1, 10), (10, 0), (10, -1), (True, 10), (10, False), (2.5, 10)):
            with self.subTest(width=width, height=height), self.assertRaisesRegex(ValueError, "positive integer"):
                renderer.render(SHADER, width, height, 0.0)

    def test_empty_shader_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            WebGL2Renderer().render(" \n ", 10, 10, 0.0)

    def test_non_finite_time_is_rejected(self) -> None:
        renderer = WebGL2Renderer()
        for time in (float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(time=time), self.assertRaisesRegex(ValueError, "finite number"):
                renderer.render(SHADER, 10, 10, time)

    def test_render_requires_an_open_renderer(self) -> None:
        renderer = WebGL2Renderer()
        renderer.close()
        renderer.close()
        with self.assertRaises(RenderError) as caught:
            renderer.render(SHADER, 10, 10, 0.0)
        self.assertEqual(caught.exception.stage, "init")
        self.assertIn("with block", caught.exception.log)
