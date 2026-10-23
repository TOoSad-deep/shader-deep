"""管理本地页面提交的子进程与制品, 不改变分析和生成协议."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from shader_deep.infrastructure.storage.report_package import read_sketch

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 4096
TEXT_LIMIT = 24_000
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
SUFFIXES = {".png", ".glsl", ".json", ".md", ".log", ".txt", ".jsonl"}


class Submission(BaseModel):
    """界面提交的有限参数, 不接受命令或任意本地路径."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    mode: Literal["generate", "analyze", "report"]
    prompt: str = Field(min_length=1, max_length=8000)
    image: str = Field(default="", max_length=MAX_IMAGE_BYTES * 4 // 3)
    width: int = Field(default=512, ge=1, le=2048)
    height: int = Field(default=512, ge=1, le=2048)
    attempts: int = Field(default=3, ge=1, le=10)
    perspectives: int = Field(default=3, ge=2, le=3)
    source: str = ""
    sketch: str = ""
    alternative: int | None = Field(default=None, ge=0)
    background: str = Field(default="透明", min_length=1, max_length=1000)


def _png(encoded: str) -> bytes:
    try:
        data = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            _verify_png(data)
    except (OSError, binascii.Error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        msg = "无法读取 PNG 图片, 请重新选择"
        raise ValueError(msg) from exc
    return data


def _verify_png(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or max(image.size) > MAX_DIMENSION or len(data) > MAX_IMAGE_BYTES:
            msg = "请选择不超过 12 MB、边长不超过 4096 的 PNG 图片"
            raise ValueError(msg)
        image.verify()


def _tail(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - TEXT_LIMIT))
        return stream.read(TEXT_LIMIT).decode("utf-8", errors="replace")


def _object(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT.validate_json(path.read_bytes())
    except (OSError, ValueError):
        return {}


@dataclass
class Job:
    """页面会话内的一次独立运行, 子进程退出后仍保留制品."""

    identity: str
    root: Path
    request: Submission
    process: subprocess.Popen[bytes]
    cancelled: bool = False

    def status(self) -> str:
        """读取进程和业务终态, 保留部分完成与预算耗尽状态."""
        code = self.process.poll()
        if self.cancelled:
            return "cancelled"
        if code is None:
            return "running"
        result = _object(self.root / "stdout.txt").get("status") if self.request.mode == "analyze" else self.details().get("stop_reason")
        return str(result) if result and code in (0, 1) else "completed" if code == 0 else "error"

    def details(self) -> dict[str, JsonValue]:
        """读取既有运行快照, 不把候选存在当作已选中."""
        paths = list((self.root / "output").glob("run-*/run.json"))
        return _object(paths[0]) if paths else {}

    def report(self) -> Path | None:
        """仅返回本任务内已结束分析发布的报告目录."""
        if self.request.mode != "analyze" or self.process.poll() is None:
            return None
        value = _object(self.root / "stdout.txt").get("report_dir")
        if not isinstance(value, str):
            return None
        path = Path(value).resolve()
        return path if path.is_relative_to(self.root) and (path / "manifest.json").is_file() else None

    def file(self, relative: str) -> Path:
        """限制下载范围, 阻止路径穿越及指向任务外的符号链接."""
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.suffix not in SUFFIXES or not path.is_file():
            msg = "文件不存在或不属于本次任务"
            raise ValueError(msg)
        return path

    def url(self, path: Path) -> str:
        """生成当前任务制品的相对下载地址."""
        return f"/files/{self.identity}/{quote(path.relative_to(self.root).as_posix())}"

    def snapshot(self) -> dict[str, JsonValue]:
        """返回状态、日志与可下载制品, 不传输进程环境变量."""
        files: list[JsonValue] = [
            {"name": path.relative_to(self.root).as_posix(), "url": self.url(path)}
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and path.suffix in SUFFIXES and path.resolve().is_relative_to(self.root)
        ]
        report = self.report()
        return {
            "id": self.identity,
            "mode": self.request.mode,
            "prompt": self.request.prompt,
            "status": self.status(),
            "directory": str(self.root),
            "files": files,
            "log": _tail(self.root / "stderr.log"),
            "details": self.details(),
            "report": (report / "README.md").read_text(encoding="utf-8") if report else "",
            "sketches": json.loads((report / "sketches.json").read_text(encoding="utf-8")) if report else [],
        }

    def stop(self) -> None:
        """终止本任务及其浏览器子进程, 保留已有文件并回收进程."""
        if self.process.poll() is not None:
            return
        self.cancelled = True
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        except ProcessLookupError:
            self.process.wait()


def _command(request: Submission, root: Path, report: Path | None) -> list[str]:
    module = "analysis" if request.mode == "analyze" else "generation"
    command = [sys.executable, "-u", "-m", f"shader_deep.cli.{module}"]
    if report is not None:
        command += [request.prompt, "--report", str(report), "--sketch", request.sketch, "--background", request.background]
        if request.alternative is not None:
            command += ["--alternative", str(request.alternative)]
    else:
        command += [str(root / "reference.png"), request.prompt]
    command += ["--output-dir", str(root / "output")]
    if request.mode == "analyze":
        return [*command, "--max-tasks", str(request.perspectives)]
    return [*command, "--width", str(request.width), "--height", str(request.height), "--max-attempts", str(request.attempts)]


class Jobs:
    """串行启动本地任务, 互斥保护多个浏览器标签的提交."""

    def __init__(self, root: Path) -> None:
        """记录独立输出目录, 不扫描或恢复旧会话."""
        self.root = root.resolve()
        self.items: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.closed = False

    def start(self, request: Submission) -> Job:
        """校验输入后启动固定 CLI, 同时最多执行一个任务.

        Args:
            request: 已校验的页面参数.

        Returns:
            可轮询的独立任务.

        Raises:
            ValueError: 已有运行中任务或输入/报告无效.
        """
        with self.lock:
            if self.closed:
                msg = "服务正在关闭, 不再接受新任务"
                raise ValueError(msg)
            if any(job.process.poll() is None for job in self.items.values()):
                msg = "已有任务正在运行, 请等待完成或先停止任务"
                raise ValueError(msg)
            report = self._source(request)
            data = _png(request.image) if report is None else (report / "reference.png").read_bytes()
            self.root.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="web-", dir=self.root))
            (root / "reference.png").write_bytes(data)
            with (root / "stdout.txt").open("wb") as output, (root / "stderr.log").open("wb") as errors:
                process = subprocess.Popen(  # noqa: S603  # 固定 Python 模块和参数列表, 不经 shell 执行页面输入.
                    _command(request, root, report),
                    stdout=output,
                    stderr=errors,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            job = Job(root.name, root, request.model_copy(update={"image": ""}), process)
            self.items[job.identity] = job
            return job

    def _source(self, request: Submission) -> Path | None:
        if request.mode != "report":
            return None
        job = self.items.get(request.source)
        report = job.report() if job is not None else None
        if report is None:
            msg = "请先完成分析, 再选择该分析报告中的草图"
            raise ValueError(msg)
        read_sketch(report, request.sketch, alternative=request.alternative)
        return report

    def close(self) -> None:
        """服务器退出时停止仍在运行的任务."""
        with self.lock:
            self.closed = True
            for job in self.items.values():
                job.stop()
