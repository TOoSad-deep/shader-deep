"""无网络核对盲评消息与原生 JSON Schema 实际进入兼容接口载荷."""

from __future__ import annotations

import json
from typing import cast
from unittest import TestCase

import httpx2
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from shader_deep.experiments.parameter_evaluation import parse_judge_result
from shader_deep.experiments.parameter_evaluation._judge import prepare_judge_request


class NativeJudgeTransportTests(TestCase):
    def test_blind_images_and_full_strict_schema_reach_actual_serialized_request(self) -> None:
        pair_id = "a" * 64
        prepared = prepare_judge_request(b"reference fixture", b"candidate fixture", {"pair_id": pair_id})
        verdict = {
            "pair_id": pair_id,
            "overall": "needs_work",
            "criteria": {
                name: {"status": "needs_work", "evidence": [{"rectangle": [330, 145, 695, 435], "observation": "可定位的测试差异"}]}
                for name in ("geometry_layout", "overlap_transparency", "color_placement", "edges_transitions")
            },
        }
        requests: list[dict] = []

        def response(request: httpx2.Request) -> httpx2.Response:
            requests.append(json.loads(request.content))
            return httpx2.Response(
                200,
                json={
                    "id": "offline-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "offline-model",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(verdict)}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        with httpx2.Client(transport=httpx2.MockTransport(response)) as transport:
            model = ChatOpenAI(
                model="offline-model",
                api_key="offline-placeholder",
                base_url="https://offline.invalid/v1",
                http_client=transport,
                max_retries=0,
                use_responses_api=False,
            )
            result = model.bind(response_format=prepared["response_format"]).invoke(
                [HumanMessage(content=cast("list[dict]", prepared["messages"])[0]["content"])]
            )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["messages"], prepared["messages"])
        self.assertNotIn("tools", requests[0])
        native = requests[0]["response_format"]
        self.assertEqual(native["type"], "json_schema")
        self.assertIs(native["json_schema"]["strict"], expr2=True)
        self.assertEqual(native["json_schema"]["schema"], prepared["schema"])
        self.assertEqual(result.response_metadata["finish_reason"], "stop")
        self.assertIsNone(parse_judge_result(str(result.content), pair_id)["parse_error"])
