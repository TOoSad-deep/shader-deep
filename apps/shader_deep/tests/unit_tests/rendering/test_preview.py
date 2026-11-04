"""常驻预览的配置校验和未启动生命周期不需要浏览器."""

from threading import current_thread
from unittest import TestCase
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from shader_deep.rendering import PreviewSnapshot, RenderError, ShaderPreview

SHADER = "uniform float RED; void mainImage(out vec4 c, in vec2 p) { c = vec4(RED); }"


class PreviewInputTests(TestCase):
    def test_invalid_configuration_fails_before_starting_browser(self) -> None:
        for spec in (
            {"min": 0.0, "max": 1.0},
            {"min": 2.0, "max": 1.0, "initial": 1.5},
            {"min": 0.0, "max": 1.0, "initial": 2.0},
            {"min": 0.0, "max": float("inf"), "initial": 0.0},
            {"min": 0.0, "max": 1.0, "initial": True},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                ShaderPreview(SHADER, parameters={"RED": spec})
        with self.assertRaises(ValueError):
            ShaderPreview(SHADER, parameters={}, width=0)

    def test_calls_require_open_session_and_close_is_idempotent(self) -> None:
        preview = ShaderPreview(SHADER, parameters={"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}})
        with self.assertRaises(RenderError):
            preview.capture()
        preview.close()
        preview.close()
        with self.assertRaises(RenderError):
            preview.set_parameters({"RED": 1.0})
        with self.assertRaises(RenderError):
            preview.__enter__()

    def test_snapshot_owns_a_readonly_copy_of_parameters(self) -> None:
        values = {"RED": 0.5}
        snapshot = PreviewSnapshot(png=b"png", parameters=values, revision=1, time=0.0, width=1, height=1)
        values["RED"] = 1.0
        self.assertEqual(snapshot.parameters["RED"], 0.5)
        with self.assertRaises(TypeError):
            snapshot.parameters["RED"] = 0.0

    def test_cleanup_failure_preserves_primary_error_and_standalone_failure(self) -> None:
        preview = ShaderPreview(SHADER, parameters={})
        primary = RuntimeError("original model failure")
        cleanup = RenderError("driver closed", stage="close")
        with patch.object(preview, "close", side_effect=cleanup):
            preview.__exit__(type(primary), primary, None)
            self.assertIn("driver closed", primary.__notes__[0])
            with self.assertRaises(RenderError) as caught:
                preview.__exit__(None, None, None)
            self.assertIs(caught.exception, cleanup)

    def test_parameter_specifications_are_independent_copies(self) -> None:
        preview = ShaderPreview(SHADER, parameters={"RED": {"min": 0.0, "max": 1.0, "initial": 0.0}})
        displayed = preview.parameter_specs
        displayed["RED"]["max"] = 9.0
        self.assertEqual(preview.parameter_specs["RED"]["max"], 1.0)


class PreviewStartupTests(TestCase):
    def setUp(self) -> None:
        self.preview = ShaderPreview(SHADER, parameters={})
        self.addCleanup(self.preview.close)
        self.manager = MagicMock()
        self.browser = self.manager.__enter__.return_value.chromium.launch.return_value
        driver = patch("shader_deep.rendering._preview_browser.sync_playwright", return_value=self.manager)
        driver.start()
        self.addCleanup(driver.stop)
        request = patch.object(self.preview._browser, "_request", return_value={})
        self.request = request.start()
        self.addCleanup(request.stop)

    def test_compile_error_survives_multiple_cleanup_failures(self) -> None:
        primary = RenderError("original compile log", stage="fragment_compile")
        workers = []

        def fail_capture(_method: str) -> None:
            workers.append(current_thread())
            raise primary

        self.request.side_effect = fail_capture
        self.browser.close.side_effect = PlaywrightError("browser cleanup failed")
        self.manager.__exit__.side_effect = OSError("driver cleanup failed")
        with self.assertRaises(RenderError) as caught:
            self.preview.__enter__()
        self.assertIs(caught.exception, primary)
        self.assertEqual((primary.stage, primary.log), ("fragment_compile", "original compile log"))
        self.assertEqual(len(primary.__notes__), 2)
        self.assertTrue(any("browser cleanup failed" in note for note in primary.__notes__))
        self.assertTrue(any("driver cleanup failed" in note for note in primary.__notes__))
        self.browser.close.assert_called_once()
        self.manager.__exit__.assert_called_once()
        self.assertIsNone(self.preview._worker)
        self.assertIsNone(self.preview._browser._resources)
        self.assertIsNone(self.preview._browser._page)
        self.assertFalse(workers[0].is_alive())

    def test_driver_failure_retains_init_contract_and_visible_cleanup_notes(self) -> None:
        for primary in (PlaywrightError("page configuration failed"), OSError("page file unavailable")):
            with self.subTest(primary=type(primary).__name__):
                preview = ShaderPreview(SHADER, parameters={})
                self.browser.new_page.side_effect = primary
                self.browser.close.side_effect = PlaywrightError("secondary close failure")
                with self.assertRaises(RenderError) as caught:
                    preview.__enter__()
                self.assertEqual(caught.exception.stage, "init")
                self.assertEqual(caught.exception.log, str(primary))
                self.assertIs(caught.exception.__cause__, primary)
                self.assertIn("secondary close failure", caught.exception.__notes__[0])
                self.assertIsNone(preview._worker)

    def test_initialization_interrupt_is_preserved_and_resources_released(self) -> None:
        primary = KeyboardInterrupt("initialization interrupted")
        self.request.side_effect = primary
        self.browser.close.side_effect = PlaywrightError("secondary close failure")
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.preview.__enter__()
        self.assertIs(caught.exception, primary)
        self.assertIn("secondary close failure", primary.__notes__[0])
        self.browser.close.assert_called_once()
        self.manager.__exit__.assert_called_once()
        self.assertIsNone(self.preview._worker)

    def test_success_keeps_resources_until_close_and_reports_close_failure(self) -> None:
        self.preview.__enter__()
        self.browser.close.assert_not_called()
        self.manager.__exit__.assert_not_called()
        self.browser.close.side_effect = PlaywrightError("close transport failed")
        with self.assertRaises(RenderError) as caught:
            self.preview.close()
        self.assertEqual(caught.exception.stage, "close")
        self.assertIn("close transport failed", caught.exception.log)
        self.browser.close.assert_called_once()
        self.manager.__exit__.assert_called_once()
        self.assertIsNone(self.preview._worker)
        self.preview.close()

    def test_outer_startup_cleanup_failure_preserves_primary(self) -> None:
        primary = RenderError("startup failed", stage="init")
        cleanup = RenderError("outer cleanup failed", stage="close")
        with (
            patch.object(self.preview._browser, "start", side_effect=primary),
            patch.object(self.preview._browser, "close", side_effect=cleanup),
            self.assertRaises(RenderError) as caught,
        ):
            self.preview.__enter__()
        self.assertIs(caught.exception, primary)
        self.assertIn("outer cleanup failed", primary.__notes__[0])
        self.assertIsNone(self.preview._worker)
