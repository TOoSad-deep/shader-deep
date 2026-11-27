"""历史展示实验的本地证据记录, 不发起网络或浏览器操作."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from scripts._history_ab_policy import ALLOWED_TOOLS

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    import httpx2
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult

OUTPUT_LIMIT = 8192


def sha256(data: bytes) -> str:
    """计算原始字节的材料身份."""
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    """返回带时区的 UTC 时间."""
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    """同目录替换 JSON, 避免中断留下半个快照."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def safe(value: object) -> str:
    """只为持久化诊断消除当前进程的令牌, 不输出环境变量."""
    text = str(value)
    for name, secret in os.environ.items():
        if name.endswith(("API_KEY", "TOKEN")) and secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def image_evidence(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """只保留请求中的图片哈希及邻接元数据, 不记录凭据或载荷全文."""
    images: list[dict[str, object]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        metadata = None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                try:
                    metadata = json.loads(str(block["text"]))
                except ValueError:
                    metadata = None
            elif block.get("type") == "image_url":
                png = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
                images.append({"role": message["role"], "sha256": sha256(png), "bytes": len(png), "metadata": metadata})
    return images


class Evidence(BaseCallbackHandler):
    """记录实际 HTTP 尝试、模型/工具回调及每次成功截图, 包括失败运行."""

    raise_error = True

    def __init__(self, directory: Path, reference_sha256: str, *, limit: int = 18) -> None:
        """创建独立运行记录; HTTP hook 是请求硬预算的第二道边界."""
        self.directory = directory
        self.reference_sha256 = reference_sha256
        self.limit = limit
        self.lock = RLock()
        self.http_requests: list[dict[str, object]] = []
        self.model_responses: list[dict[str, object]] = []
        self.tools: list[dict[str, object]] = []
        self.snapshots: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.tool_names: dict[str, str] = {}

    def event(self, kind: str, **data: object) -> None:
        """追加可恢复的事件, 每次写入后关闭文件."""
        with self.lock, (self.directory / "events.jsonl").open("a") as output:
            output.write(json.dumps({"at": now(), "kind": kind, **data}, ensure_ascii=False, default=str) + "\n")

    def request(self, request: httpx2.Request) -> None:
        """在真正发送前实施硬上限, 不把被阻止的第十九次算作实际尝试."""
        with self.lock:
            if len(self.http_requests) >= self.limit:
                self.event("http_budget_blocked", limit=self.limit)
                msg = f"HTTP request attempt limit reached ({self.limit})"
                raise RuntimeError(msg)
            data = json.loads(request.content)
            tools = [item.get("function", {}).get("name") for item in data.get("tools", [])]
            images = image_evidence(data.get("messages", []))
            if request.url.host != "openrouter.ai" or request.method != "POST":
                msg = "Unexpected model request destination"
                raise RuntimeError(msg)
            if set(tools) != ALLOWED_TOOLS:
                msg = "Frozen experiment tool policy violated"
                raise RuntimeError(msg)
            policy = data.get("provider", {})
            if policy.get("only") != ["coreweave/nvfp4"] or policy.get("allow_fallbacks") is not False:
                msg = "Frozen provider routing policy violated"
                raise RuntimeError(msg)
            if data.get("max_completion_tokens", data.get("max_tokens")) != OUTPUT_LIMIT:
                msg = "Frozen output token budget violated"
                raise RuntimeError(msg)
            if not any(item["role"] == "user" and item["sha256"] == self.reference_sha256 for item in images):
                msg = "Frozen reference missing from request"
                raise RuntimeError(msg)
            entry = {
                "number": len(self.http_requests) + 1,
                "at": now(),
                "model": data["model"],
                "tools": tools,
                "images": images,
                "output_limit": data.get("max_completion_tokens", data.get("max_tokens")),
            }
            self.http_requests.append(entry)
            self.event("http_request", **entry)
            print(self.directory.name, "request", entry["number"], flush=True)  # noqa: T201  # 长运行 CLI 显示真实请求进度.

    def response(self, response: httpx2.Response) -> None:
        """读取非流式响应的提供方和用量; 失败/缺失用量保持未知."""
        with self.lock:
            entry = self.http_requests[-1]
            entry["http_status"] = response.status_code
            if "json" in response.headers.get("content-type", ""):
                body = json.loads(response.read())
                entry.update(
                    provider=body.get("provider"),
                    response_id=body.get("id"),
                    usage=body.get("usage"),
                    finish_reasons=[item.get("finish_reason") for item in body.get("choices", [])],
                )
            self.event("http_response", **entry)

    def on_chat_model_start(self, serialized: dict[str, object], messages: list[list[BaseMessage]], **kwargs: object) -> None:
        """回调开始仅是逻辑事件, 不充当实际请求数量."""
        del serialized, messages
        self.event("model_start", run_id=kwargs.get("run_id"), parent_run_id=kwargs.get("parent_run_id"))

    def on_llm_end(self, response: LLMResult, **kwargs: object) -> None:
        """保留模型原始结束原因、工具请求和已报告用量."""
        for batch in response.generations:
            for generation in batch:
                message = getattr(generation, "message", None)
                if isinstance(message, AIMessage):
                    entry: dict[str, object] = {
                        "http_attempt": len(self.http_requests),
                        "run_id": str(kwargs.get("run_id")),
                        "content": safe(message.content),
                        "tool_calls": message.tool_calls,
                        "response_metadata": message.response_metadata,
                        "usage": message.usage_metadata,
                    }
                    self.model_responses.append(entry)
                    self.event("model_response", **entry)

    def on_llm_error(self, error: BaseException, **kwargs: object) -> None:
        """保留请求异常, 不将失败响应修饰为正常结束."""
        self.event("model_error", run_id=kwargs.get("run_id"), error_type=type(error).__name__, message=safe(error))

    def on_tool_start(self, serialized: dict[str, object], input_str: str, *, run_id: UUID, **kwargs: object) -> None:
        """保存工具名称与图内运行标识."""
        del kwargs
        self.tool_names[str(run_id)] = str(serialized.get("name"))
        self.event("tool_start", run_id=run_id, name=serialized.get("name"), input=safe(input_str))

    def on_tool_end(self, output: object, **kwargs: object) -> None:
        """保存工具图片原字节及完整文本, 不重绘或读取浏览器."""
        if not isinstance(output, ToolMessage):
            msg = "Expected ToolMessage receipt"
            raise TypeError(msg)
        entry: dict[str, object] = {
            "name": output.name,
            "status": output.status,
            "call_id": output.tool_call_id,
            "http_attempt": len(self.http_requests),
            "run_id": str(kwargs.get("run_id")),
        }
        blocks = [{"type": "text", "text": output.content}] if isinstance(output.content, str) else output.content
        texts, images = [], []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(safe(block["text"]))
            elif block.get("type") == "image_url":
                png = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
                filename = f"tool-{len(self.tools) + 1:02d}-{len(images) + 1:02d}.png"
                (self.directory / filename).write_bytes(png)
                images.append({"file": filename, "sha256": sha256(png)})
        entry.update(texts=texts, images=images)
        self.tools.append(entry)
        self.event("tool_end", **entry)

    def on_tool_error(self, error: BaseException, **kwargs: object) -> None:
        """将致命工具错误保留到同一证据流."""
        entry: dict[str, object] = {"name": self.tool_names.get(str(kwargs.get("run_id"))), "status": "fatal_error", "error": safe(error)}
        self.tools.append(entry)
        self.event("tool_error", **entry)

    def summary(self) -> dict[str, object]:
        """返回当前证据快照, 保留未知成本和失败尝试."""
        return {
            "http_requests": self.http_requests,
            "model_responses": self.model_responses,
            "tool_results": self.tools,
            "snapshots": self.snapshots,
            "successful_updates": self.updates,
            "http_attempts": len(self.http_requests),
        }
