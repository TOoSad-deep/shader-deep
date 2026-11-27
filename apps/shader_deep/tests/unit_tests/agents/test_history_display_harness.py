"""历史展示实验的请求硬上限和执行权限边界."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import httpx2
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from scripts._history_ab_evidence import Evidence, sha256
from scripts._history_ab_policy import ALLOWED_TOOLS, ExperimentTools


class HistoryHarnessTests(unittest.TestCase):
    def test_eighteen_actual_attempts_and_blocked_nineteenth_are_distinct(self) -> None:
        with TemporaryDirectory() as temporary:
            reference = b"fixed image bytes"
            evidence = Evidence(Path(temporary), sha256(reference))
            data = {
                "model": "z-ai/glm-5.3-flash",
                "max_tokens": 8192,
                "provider": {"only": ["coreweave/nvfp4"], "allow_fallbacks": False},
                "tools": [{"type": "function", "function": {"name": name}} for name in ALLOWED_TOOLS],
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(reference).decode()}}],
                    }
                ],
            }
            request = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions", content=json.dumps(data))
            for _ in range(18):
                evidence.request(request)
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                evidence.request(request)
            self.assertEqual(len(evidence.http_requests), 18)
            self.assertIn("http_budget_blocked", (Path(temporary) / "events.jsonl").read_text())

    def test_extra_schema_tool_never_reaches_provider(self) -> None:
        middleware = ExperimentTools()
        request = ModelRequest(
            model=FakeListChatModel(responses=["unused"]), messages=[], tools=[{"name": name} for name in (*ALLOWED_TOOLS, "task", "write_file")]
        )
        received = []

        def handler(filtered: ModelRequest) -> ModelResponse:
            received.extend(filtered.tools)
            return ModelResponse(result=[AIMessage(content="done")])

        middleware.wrap_model_call(request, handler)
        self.assertEqual({tool["name"] for tool in received}, ALLOWED_TOOLS)

    def test_hallucinated_task_is_rejected_before_tool_dispatch(self) -> None:
        middleware = ExperimentTools()
        request = ModelRequest(model=FakeListChatModel(responses=["unused"]), messages=[])
        response = ModelResponse(result=[AIMessage(content="", tool_calls=[{"id": "outside", "name": "task", "args": {}}])])
        with self.assertRaisesRegex(RuntimeError, "outside"):
            middleware.wrap_model_call(request, lambda _: response)

    def test_tool_execution_boundary_blocks_task_and_preserves_allowed_tool(self) -> None:
        middleware = ExperimentTools()
        forbidden = Mock(tool_call={"name": "task"})
        handler = Mock(return_value=ToolMessage(content="ok", tool_call_id="allowed"))
        with self.assertRaisesRegex(RuntimeError, "blocked"):
            middleware.wrap_tool_call(forbidden, handler)
        handler.assert_not_called()
        allowed = Mock(tool_call={"name": "get_trial_history"})
        middleware.wrap_tool_call(allowed, handler)
        handler.assert_called_once_with(allowed)
