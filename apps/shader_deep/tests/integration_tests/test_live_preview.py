"""真实浏览器验证持续预览、滑块更新、参数快照和关闭后的拒绝行为."""

import base64
from importlib.resources import files
from io import BytesIO
from unittest import TestCase

from PIL import Image
from playwright.sync_api import sync_playwright


class LivePreviewTests(TestCase):
    def test_external_updates_sync_controls_and_cached_page_resumes(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True, chromium_sandbox=True)
            try:
                page = browser.new_page()
                page.goto(files("shader_deep.rendering").joinpath("preview.html").as_uri())
                page.evaluate("shaderPreview.setParameters({GLOW_STRENGTH: 0, LINE_WIDTH: 0.04})")
                self.assertEqual(page.locator("#GLOW_STRENGTH").input_value(), "0")
                self.assertEqual(page.locator("#GLOW_STRENGTH-value").inner_text(), "0")
                self.assertEqual(page.locator("#LINE_WIDTH").input_value(), "0.04")
                self.assertEqual(page.locator("#LINE_WIDTH-value").inner_text(), "0.04")
                before = page.evaluate("shaderPreview.capture()")
                # 统计真实绘制次数, 验证缓存暂停期间不绘制, 恢复后重新持续绘制.
                page.evaluate("""() => {
                    const gl = document.querySelector('canvas').getContext('webgl2');
                    const draw = gl.drawArrays.bind(gl);
                    window.drawCount = 0;
                    gl.drawArrays = (...args) => { window.drawCount++; return draw(...args); };
                }""")
                for _ in range(2):
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
                    paused = page.evaluate("drawCount")
                    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    self.assertEqual(page.evaluate("drawCount"), paused)
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
                    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    self.assertGreater(page.evaluate("drawCount"), paused)
                    self.assertEqual(page.evaluate("shaderPreview.capture()"), before)
                page.locator("#reset").click()
                self.assertEqual(page.locator("#GLOW_STRENGTH").input_value(), "1.2")
                self.assertEqual(page.locator("#GLOW_STRENGTH-value").inner_text(), "1.2")
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false}))")
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
                self.assertIn("closed", page.evaluate("() => { try { shaderPreview.capture(); } catch (error) { return error.message; } }"))
            finally:
                browser.close()

    def test_controls_update_pixels_without_replacing_program(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True, chromium_sandbox=True)
            page = browser.new_page()
            try:
                page.goto(files("shader_deep.rendering").joinpath("preview.html").as_uri())
                self.assertIn("预览已就绪", page.locator("#status").inner_text())
                page.evaluate("window.originalProgram = document.querySelector('canvas').getContext('webgl2').getParameter(35725)")
                before = page.evaluate("shaderPreview.capture()")
                page.locator("#GLOW_STRENGTH").evaluate("slider => { slider.value = '0'; slider.dispatchEvent(new Event('input')); }")
                after = page.evaluate("shaderPreview.capture()")
                self.assertEqual(after["parameters"]["GLOW_STRENGTH"], 0.0)
                self.assertGreater(after["revision"], before["revision"])
                self.assertTrue(page.evaluate("originalProgram === document.querySelector('canvas').getContext('webgl2').getParameter(35725)"))
                with (
                    Image.open(BytesIO(base64.b64decode(before["png"].split(",", 1)[1]))) as first,
                    Image.open(BytesIO(base64.b64decode(after["png"].split(",", 1)[1]))) as second,
                ):
                    self.assertEqual(first.size, (512, 512))
                    self.assertGreater(first.convert("RGB").getpixel((420, 256))[2], second.convert("RGB").getpixel((420, 256))[2])
                rejected = page.evaluate("""() => {
                    try { shaderPreview.setParameters({GLOW_STRENGTH: 2, LINE_WIDTH: 9}); }
                    catch (error) { return error.message; }
                }""")
                self.assertIn("outside its range", rejected)
                self.assertEqual(page.evaluate("shaderPreview.capture().parameters"), after["parameters"])
                page.locator("#reset").click()
                self.assertEqual(page.evaluate("shaderPreview.capture().parameters"), before["parameters"])
                page.evaluate("shaderPreview.dispose()")
                self.assertIn("closed", page.evaluate("() => { try { shaderPreview.capture(); } catch (error) { return error.message; } }"))
            finally:
                # 在 Playwright 所在线程及其上下文退出前释放浏览器.
                browser.close()
