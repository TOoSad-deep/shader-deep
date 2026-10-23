"""本机 HTTP 路由和上传任务联通检查, 子进程替身不访问模型服务."""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep.web.server import LocalServer
from tests.unit_tests.test_web import png_bytes


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
