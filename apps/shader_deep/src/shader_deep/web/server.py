"""只绑定回环地址的本地 HTTP 界面, 限制来源、请求大小和文件范围."""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from shader_deep.web.jobs import Jobs, Submission

ASSETS = Path(__file__).parent / "assets"
MAX_BODY = 17 * 1024 * 1024


class LocalServer(ThreadingHTTPServer):
    """持有任务会话和页面操作令牌的本机服务器."""

    def __init__(self, port: int, directory: Path) -> None:
        """绑定本机端口, 输出制品只写入明确目录."""
        self.jobs = Jobs(directory)
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def server_close(self) -> None:
        """关闭服务并回收任务子进程."""
        self.jobs.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    """使用固定路由提供页面、任务快照与本会话制品."""

    server: LocalServer

    def setup(self) -> None:
        """限制客户端上传和请求的等待时间."""
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # 保留标准库覆盖方法的参数名.
        """轮询不写访问日志, 业务日志由子进程保留."""

    def _send(self, status: int, body: bytes, content: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def do_GET(self) -> None:
        """读取界面及快照, 不启动任何模型任务."""
        if not self._allowed():
            self._json(403, {"error": "请使用服务器显示的本机地址"})
            return
        try:
            self._get(unquote(urlsplit(self.path).path))
        except (KeyError, OSError, ValueError):
            self._json(404, {"error": "页面、任务或文件不存在"})

    def _get(self, path: str) -> None:
        if path == "/":
            page = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif path in {"/app.js", "/style.css"}:
            self._send(200, (ASSETS / path[1:]).read_bytes(), "text/javascript" if path.endswith(".js") else "text/css")
        elif path == "/api/jobs":
            with self.server.jobs.lock:
                self._json(200, [job.snapshot() for job in reversed(list(self.server.jobs.items.values()))])
        elif path.startswith("/files/"):
            _, _, identity, relative = path.split("/", 3)
            file = self.server.jobs.items[identity].file(relative)
            content = "image/png" if file.suffix == ".png" else "text/plain; charset=utf-8"
            self._send(200, file.read_bytes(), content)
        else:
            self._json(404, {"error": "页面不存在"})

    def do_POST(self) -> None:
        """要求页面令牌与同源请求后, 才能启动或停止任务."""
        token = self.headers.get("X-Shader-Token", "")
        if not self._allowed() or self.headers.get("Origin") != self.server.origin or not hmac.compare_digest(token, self.server.token):
            self._json(403, {"error": "页面会话已失效, 请刷新页面"})
            return
        try:
            self._post()
        except ValidationError:
            self._json(400, {"error": "参数无效, 请检查图片、要求和数值范围"})
        except (ValueError, KeyError) as exc:
            self._json(400, {"error": str(exc)})
        except OSError:
            self._json(500, {"error": "本地文件或进程启动失败, 请检查输出目录权限"})

    def _post(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY or self.headers.get("Content-Type") != "application/json":
            msg = "请求格式错误或图片过大"
            raise ValueError(msg)
        body = self.rfile.read(length)
        if self.path == "/api/jobs":
            job = self.server.jobs.start(Submission.model_validate_json(body))
            self._json(201, {"id": job.identity})
        elif self.path.startswith("/api/stop/"):
            with self.server.jobs.lock:
                self.server.jobs.items[self.path.removeprefix("/api/stop/")].stop()
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "操作不存在"})
