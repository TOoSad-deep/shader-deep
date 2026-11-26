"""渲染器输入检查不需要启动浏览器."""

from unittest import TestCase
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError, Page

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

    def test_malformed_uniforms_are_rejected_before_browser_launch(self) -> None:
        renderer = WebGL2Renderer()
        cases = (
            ({"iTime": 1}, "reserved"),
            ({"iResolution": (1, 2, 3)}, "reserved"),
            ({"u__reserved": 1}, "Invalid GLSL uniform name"),
            ({"colors[0]": 1}, "Invalid GLSL uniform name"),
            ({"u_value": (1,)}, "vector components"),
            ({"u_value": (1, float("nan"))}, "finite float32"),
            ({"u_value": 1e90}, "finite float32"),
            ({"u_value": True}, "finite float32"),
        )
        for uniforms, message in cases:
            with self.subTest(uniforms=uniforms), self.assertRaisesRegex(ValueError, message):
                renderer.render(SHADER, 10, 10, 0.0, uniforms=uniforms)


class OptimizationSessionTests(TestCase):
    def test_primary_browser_error_survives_cleanup_error(self) -> None:
        renderer, page = WebGL2Renderer(), Mock(spec=Page)
        page.evaluate.side_effect = [{"ok": True, "value": None}, PlaywrightError("draw crashed"), PlaywrightError("cleanup crashed")]
        with (
            self.assertRaisesRegex(RenderError, "draw crashed") as caught,
            patch.object(renderer, "_page", page),
            renderer.optimization_session(SHADER, 10, 10, 0.0, uniforms={}) as session,
        ):
            session.render({})
        self.assertEqual(caught.exception.stage, "browser")
        self.assertIn("cleanup crashed", "\n".join(caught.exception.__notes__))
        with self.assertRaisesRegex(RenderError, "session is closed"):
            session.read_pixels({})

    def test_cleanup_error_is_reported_when_body_succeeds(self) -> None:
        renderer, page = WebGL2Renderer(), Mock(spec=Page)
        page.evaluate.side_effect = [{"ok": True, "value": None}, PlaywrightError("cleanup crashed")]
        with (
            self.assertRaisesRegex(RenderError, "cleanup crashed"),
            patch.object(renderer, "_page", page),
            renderer.optimization_session(SHADER, 10, 10, 0.0, uniforms={}),
        ):
            pass
