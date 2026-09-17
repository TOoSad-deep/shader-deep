"""分析假模型先执行真实读取工具, 再使用请求历史中实际收到的报告."""

from __future__ import annotations

import json

from tests.unit_tests._generation_fixture import GenerationFixture, tool_results


def raw_context_payload(request: dict[str, object]) -> dict[str, object]:
    for message in request["messages"]:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "text" and block["text"].startswith('{\n  "kind": "analysis_task_context"'):
                return json.loads(block["text"])
    msg = "Missing actual analysis context"
    raise AssertionError(msg)


def context_payload(request: dict[str, object]) -> dict[str, object]:
    # 模拟模型整合本轮材料和已收到的工具历史, 不修改实际 HTTP 请求或生产 Context Builder.
    payload = raw_context_payload(request)
    if payload["task"]["lens_config"] is None:
        read = {
            item["result_id"]: item["content"]
            for item in tool_results(request)
            if item.get("status") == "read" and item.get("complete") and item.get("pointer") == ""
        }
        payload["related_results"] = [read[item["result_id"]] for item in payload.get("report_files", []) if item["result_id"] in read]
        payload["related_results"].extend({**item, "analysis_detail": None} for item in payload.get("failed_results", []))
    return payload


class AnalysisFixture(GenerationFixture):
    """现有业务用例的假模型先读完新报告; 隔离测试另用原始请求断言."""

    def response_message(self, request: dict[str, object]) -> dict[str, object]:
        payload = raw_context_payload(request)
        read = {
            item["result_id"] for item in tool_results(request) if item.get("status") == "read" and item.get("complete") and item.get("pointer") == ""
        }
        unread = [item for item in payload.get("report_files", []) if item["result_id"] not in read]
        if payload["task"]["lens_config"] is None and unread:
            message = {"role": "assistant", "content": "", "tool_calls": []}
            for index, report in enumerate(unread):
                call = self.call("read_analysis_file", {"file_path": report["file_path"], "pointer": ""})["tool_calls"][0]
                call["id"] += f"-read-{index}"
                message["tool_calls"].append(call)
            return message
        return self.response(request)
