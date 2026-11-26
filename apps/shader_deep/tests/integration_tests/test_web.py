"""本机 HTTP 路由和上传任务联通检查, 子进程替身不访问模型服务."""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from shader_deep.domain.review_items import CheckAnswer, CheckFinding, summarize_checks
from shader_deep.domain.visual_review import PixelRegion, ReviewSample
from shader_deep.infrastructure.storage.review_package import capture_review_source, write_review_package
from shader_deep.web.jobs import Job, Submission
from shader_deep.web.server import LocalServer
from tests.unit_tests.fixtures._review_fixture import review_request
from tests.unit_tests.test_web import png_bytes

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from pydantic import JsonValue


class WebHTTPTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.server = LocalServer(0, Path(self.temporary.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        self.connection.request(method, path, body, headers or {})
        response = self.connection.getresponse()
        return response.status, response.read()

    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Origin": self.server.origin, "X-Shader-Token": self.server.token}

    def archived_review_json(self) -> bytes:
        root = Path(self.temporary.name)
        reference, candidate = root / "reference.png", root / "candidate.png"
        for path, value in ((reference, 100), (candidate, 90)):
            with Image.new("RGB", (8, 8), (value, value, value)) as image:
                image.save(path)
        region = PixelRegion(left=0, top=0, right=8, bottom=8)
        request = review_request(required=("shape", "layout", "color")).model_copy(
            update={"protocol": "visual_review_v2", "samples": (ReviewSample(label="前圆", reference_region=region, rendered_region=region),)}
        )
        source = capture_review_source(reference, candidate, root / "review-run", request)
        keys = {item.dimension: item.check_id for item in source.criteria.checks}
        major = CheckFinding(
            region="中央", reference_observation="圆环开口可见", rendered_observation="开口被填平", impact="必要开口缺失", severity="major"
        )
        minor = CheckFinding(region="中央", reference_observation="浅灰", rendered_observation="略深", impact="只影响亮度", severity="minor")
        answers = (
            CheckAnswer(check_id=keys["shape"], rationale="开口不同", findings=(major,)),
            CheckAnswer(check_id=keys["layout"], rationale="动态位置无法判断", unassessed_reason="当前帧没有完整动作"),
            CheckAnswer(check_id=keys["color"], rationale="稍暗", findings=(minor,)),
        )
        submission = summarize_checks(source.criteria, source.coverage, answers, summary="整图核对")
        package = write_review_package(root / "review", source, submission, check_answers=answers)
        return (package / "review.json").read_bytes()

    def test_home_static_assets_and_empty_session(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), page)
        for path in ("/app.js", "/style.css", "/api/jobs"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.request("GET", "/.env")[0], 404)

    def test_cross_origin_token_and_host_checks_block_submission(self) -> None:
        for headers in ({}, {**self.headers(), "Origin": "https://example.com"}, {**self.headers(), "X-Shader-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/jobs", b"{}", headers)[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)
        self.assertFalse(self.server.jobs.items)

    def assert_numeric_reroute(self, page: Page, state: dict[str, JsonValue], diagnosis: dict[str, JsonValue]) -> None:
        diagnosis["items"] = [{"route": "numeric_tuning", "parameter_names": ["u_radius"], "evidence": []}]
        state["revision"] = 8
        expect(page.locator("#gate-route-numeric")).to_be_enabled()
        page.locator("#gate-action").select_option("reroute")
        page.locator("#gate-route").select_option("numeric_tuning")
        page.locator("#gate-reason").fill("检查半径")
        with page.expect_request("**/api/advance/archived") as tuned:
            page.locator("#gate-submit").click()
        self.assertEqual(tuned.value.post_data_json, {"action": "reroute", "route": "numeric_tuning", "reason": "检查半径", "expected_revision": 8})

    def test_gate_uses_archived_pair_while_another_image_is_staged(self) -> None:
        job_id = "archived"
        directory, run = "/jobs/archived", "output/run-A"
        current = {
            "id": "current",
            "preview_path": "current.png",
            "scope": "scene",
            "verdict": "needs_revision",
            "diagnosis_path": "card.json",
            "review_path": "review",
        }
        files = [
            {"name": f"{run}/{name}", "url": f"/files/{job_id}/{name}"}
            for name in ("reference.png", "current.png", "card.json", "review/review.json", "best-review/review.json")
        ]
        state: dict[str, JsonValue] = {
            "revision": 7,
            "directory": f"{directory}/{run}",
            "reference_path": "reference.png",
            "current_candidate": current,
            "best_candidate": {"id": "best", "preview_path": "current.png", "review_path": "best-review"},
        }
        job = {
            "id": job_id,
            "mode": "workflow",
            "status": "gate_b",
            "directory": directory,
            "details": state,
            "files": files,
            "workflow": {},
            "sketches": [],
        }
        png = png_bytes()
        review = self.archived_review_json()
        diagnosis: dict[str, JsonValue] = {"items": [{"route": "code_structure", "evidence": []}]}
        self.assertNotIn("action_summary", json.loads(review))
        with sync_playwright() as playwright, playwright.chromium.launch(channel="chromium", headless=True, chromium_sandbox=True) as browser:
            page = browser.new_page()
            page.route("**/api/jobs", lambda route: route.fulfill(json=[job]))
            page.route("**/files/archived/*.png", lambda route: route.fulfill(content_type="image/png", body=png))
            page.route("**/files/archived/card.json", lambda route: route.fulfill(json=diagnosis))
            page.route("**/review/review.json", lambda route: route.fulfill(content_type="application/json", body=review))
            page.route("**/best-review/review.json", lambda route: route.fulfill(content_type="application/json", body=review))
            page.route("**/api/advance/archived", lambda route: route.fulfill(json={"ok": True}))
            page.goto(self.server.origin)
            evidence = page.locator("#gate-evidence")
            expect(evidence).to_contain_text("Δ -10.0")
            expect(evidence).to_contain_text("圆环开口可见 → 开口被填平")
            expect(evidence).to_contain_text("当前帧没有完整动作")
            expect(evidence).not_to_contain_text("只影响亮度")
            page.locator("#image").set_input_files({"name": "next-job.png", "mimeType": "image/png", "buffer": png})
            expect(page.locator("#draft-image")).to_have_attribute("src", "data:image/png;base64," + base64.b64encode(png).decode())
            expect(page.locator("#reference")).to_have_attribute("src", "/files/archived/reference.png")
            page.locator("#reference").evaluate("(image) => image.decode()")
            with page.expect_response("**/best-review/review.json"):
                page.get_by_role("button", name="历史最佳", exact=True).click()
            expect(page.locator("#preview")).to_have_attribute("src", "/files/archived/current.png")
            page.locator("#gate-submit").click()
            expect(page.locator("#gate-error")).to_contain_text("请切回本轮候选")
            page.get_by_role("button", name="本轮候选", exact=True).click()
            page.locator("#preview").evaluate("(image) => image.decode()")
            page.locator("#gate-reason").fill("接受当前差异")
            with page.expect_request("**/api/advance/archived") as accepted:
                page.locator("#gate-submit").click()
            self.assertEqual(
                accepted.value.post_data_json, {"action": "accept", "candidate_id": "current", "reason": "接受当前差异", "expected_revision": 7}
            )
            page.locator("#gate-reason").clear()
            page.locator("#gate-action").select_option("reroute")
            expect(page.locator("#gate-route-numeric")).to_be_disabled()
            page.locator("#gate-route").select_option("code_structure")
            page.locator("#gate-action").select_option("revise")
            with page.expect_request("**/api/advance/archived") as revised:
                page.locator("#gate-submit").click()
            self.assertEqual(revised.value.post_data_json, {"action": "revise", "expected_revision": 7})
            self.assert_numeric_reroute(page, state, diagnosis)

    def test_gate_a_submits_two_regions_and_compares_without_accidentally_selecting_a_probe(self) -> None:
        region = {"target_region": {"left": 0, "top": 0, "right": 4, "bottom": 4}}
        sketch = {
            "id": "S1",
            "name": "基础",
            "alternatives": [{"reason": "试用其他机制"}],
            "risk_probes": [{"target_ids": [ref], "reason": "核对轮廓"} for ref in ("F1", "F2")],
        }
        job = {
            "id": "start",
            "mode": "workflow",
            "status": "gate_a",
            "directory": "/jobs/start",
            "files": [],
            "sketches": [],
            "details": {"directory": "/jobs/start/output/run-A", "revision": 4, "planning_path": "plan"},
            "workflow": {"sketches": [sketch], "criteria_retry_available": True},
        }
        with sync_playwright() as playwright, playwright.chromium.launch(channel="chromium", headless=True, chromium_sandbox=True) as browser:
            page = browser.new_page()
            page.route("**/api/jobs", lambda route: route.fulfill(json=[job]))
            page.route("**/api/advance/start", lambda route: route.fulfill(json={"ok": True}))
            page.goto(self.server.origin)
            page.locator("#gate-action").select_option("run_probe")
            page.locator("#gate-sketch").select_option("S1")
            page.locator("#gate-probe").select_option("0")
            page.locator("#gate-second").select_option("1")
            for field in ("gate-probe-context", "gate-second-context"):
                page.locator(f"#{field}").fill(json.dumps(region))
            with page.expect_request("**/api/advance/start") as local:
                page.locator("#gate-submit").click()
            self.assertEqual(local.value.post_data_json["probes"], [{**region, "probe_id": i} for i in range(2)])
            page.locator("#gate-action").select_option("compare_plans")
            page.locator("#gate-rival").select_option(index=1)
            with page.expect_request("**/api/advance/start") as comparison:
                page.locator("#gate-submit").click()
            payload = comparison.value.post_data_json
            self.assertEqual(payload["comparison_plan"], {"sketch_id": "S1", "alternative_index": 0})
            self.assertNotIn("probe_id", payload)

            page.locator("#gate-action").select_option("retry_criteria")
            expect(page.locator("#gate-reason")).to_have_attribute("required", "")
            page.locator("#gate-reason").fill("补充边缘核对")
            with page.expect_request("**/api/advance/start") as redraft:
                page.locator("#gate-submit").click()
            self.assertEqual(redraft.value.post_data_json, {"action": "retry_criteria", "reason": "补充边缘核对", "expected_revision": 4})

            job["status"] = "blocked"
            job["details"]["revision"] = 5
            expect(page.locator("#gate-title")).to_have_text("标准草拟受阻")
            expect(page.locator("#gate-reason-field")).to_be_hidden()
            with page.expect_request("**/api/advance/start") as retry:
                page.locator("#gate-submit").click()
            self.assertEqual(retry.value.post_data_json, {"action": "retry_criteria", "expected_revision": 5})

    def test_upload_poll_download_and_invalid_requests(self) -> None:
        payload = {"mode": "generate", "prompt": "画一个球", "image": base64.b64encode(png_bytes()).decode()}
        with patch("shader_deep.web.jobs._command", return_value=[sys.executable, "-c", "pass"]):
            status, data = self.request("POST", "/api/jobs", json.dumps(payload).encode(), self.headers())
        self.assertEqual(status, 201)
        identity = json.loads(data)["id"]
        self.server.jobs.items[identity].process.wait(timeout=10)
        status, data = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)[0]["status"], "completed")
        self.assertEqual(self.request("GET", f"/files/{identity}/reference.png"), (200, png_bytes()))
        self.assertEqual(self.request("GET", f"/files/{identity}/%2e%2e/%2e%2e/.env")[0], 404)
        for body in (b"not json", b"{}", b'{"mode":"generate","prompt":""}'):
            self.assertEqual(self.request("POST", "/api/jobs", body, self.headers())[0], 400)

    def test_analysis_migration_error_is_specific_and_other_errors_stay_generic(self) -> None:
        payload = {"mode": "analyze", "prompt": "观察全图", "perspectives": 2}
        with patch("shader_deep.web.jobs.subprocess.Popen") as start:
            status, data = self.request("POST", "/api/jobs", json.dumps(payload).encode(), self.headers())
        self.assertEqual(status, 400)
        self.assertIn("单 Agent", json.loads(data)["error"])
        self.assertIn("perspectives", json.loads(data)["error"])
        start.assert_not_called()
        self.assertFalse(self.server.jobs.items)
        status, data = self.request("POST", "/api/jobs", b'{"mode":"generate","prompt":""}', self.headers())
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], "参数无效, 请检查图片、要求和数值范围")

    def test_corrupt_workflow_artifact_keeps_history_and_gate_validation_usable(self) -> None:
        root = Path(self.temporary.name).resolve() / "workflow"
        run = root / "output" / "run-A"
        run.mkdir(parents=True)
        (run / "workflow.json").write_text(json.dumps({"phase": "gate_a", "revision": 1, "criteria_path": "broken.json"}))
        (run / "broken.json").write_text("invalid json")
        request = Submission(mode="workflow", prompt="画圆")
        self.server.jobs.items["workflow"] = Job("workflow", root, request, Mock(poll=Mock(return_value=0)))
        status, data = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)[0]["status"], "gate_a")
        self.assertNotIn("criteria", json.loads(data)[0]["workflow"])

        invalid_text = "不要在错误响应回显这段内容"
        decision = {"action": "generate_scene", "expected_revision": 1, "sketch_id": "S1", "criteria": {"target": [invalid_text]}}
        status, data = self.request("POST", "/api/advance/workflow", json.dumps(decision).encode(), self.headers())
        self.assertEqual(status, 400)
        message = json.loads(data)["error"]
        self.assertIn("criteria.target", message)
        self.assertNotIn(invalid_text, message)
