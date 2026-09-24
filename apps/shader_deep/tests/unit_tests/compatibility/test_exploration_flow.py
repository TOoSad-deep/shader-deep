"""保留原按需会话的读取、测量及事务兼容回归; 默认流程见 test_managed_integration."""

from __future__ import annotations

import base64
import json
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import BytesIO, StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from PIL import Image
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import StructuredTool

from shader_deep.agents.exploration.prompts import EXPLORATION_PROMPT
from shader_deep.agents.outline.prompts import OUTLINE_PROMPT
from shader_deep.cli.analysis import main
from shader_deep.compatibility.ondemand.prompts import INTEGRATION_PROMPT
from shader_deep.compatibility.ondemand.session import ExplorationSession
from shader_deep.domain.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.domain.library.models import ExplorationReport, PossibilityLibrary
from shader_deep.domain.tasks import ResultRecord, TargetRecord, TaskRecord
from shader_deep.workflows.analysis import run_analysis, run_analysis_task
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results

if TYPE_CHECKING:
    from shader_deep.workflows.outcomes import AnalysisOutcome


def context_payload(request: dict[str, object]) -> dict[str, object]:
    for message in reversed(request["messages"]):
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "text" and block["text"].startswith('{"user_request":'):
                return json.loads(block["text"])
    msg = "Missing new analysis business context"
    raise AssertionError(msg)


def control_payload(request: dict[str, object]) -> dict[str, object]:
    for message in reversed(request["messages"]):
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "text" and block["text"].startswith("运行控制反馈: "):
                return json.loads(block["text"].split(": ", 1)[1])
    return {}


def outline_arguments() -> dict[str, object]:
    return {
        "outline": {"elements": [{"id": "E1", "name": "条带", "scope": "画面中部", "salient_features": ["边缘较亮"]}], "relations": []},
        "directions": ["平面组织", "空间形态"],
    }


def report_arguments(direction: str) -> dict[str, object]:
    return {
        "report": {
            "sketch_library": [
                {
                    "id": "S1",
                    "name": "候选组成",
                    "element_ids": ["E1"],
                    "composition": ["重复公共条带"],
                    "feature_refs": [{"feature_id": "F1", "candidate_ids": ["C1"]}],
                }
            ],
            "feature_library": [
                {
                    "id": "F1",
                    "name": "亮边",
                    "element_ids": ["E1"],
                    "appearance": "边缘更亮",
                    "candidates": [
                        {"id": "C1", "mechanism": "独有机制-" + direction, "reasoning": "需要渲染比较"},
                        {"id": "C2", "mechanism": "未被草图引用-" + direction, "reasoning": "仍有尝试价值"},
                    ],
                }
            ],
            "relation_library": [],
        },
    }


class OnDemandCompatibilityTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        # 这些用例专门保护仍被底层复用的按需协议; 不再把它们计作新默认编排的验证.
        session = patch("shader_deep.workflows.analysis.ManagedIntegrationSession", ExplorationSession)
        session.start()
        self.addCleanup(session.stop)
        self.analysis_options = AnalysisOptions(output_dir=self.root / "analysis", max_main_calls=30, max_worker_calls=3, max_parallel=2)
        self.stopped_direction: str | None = None
        self.response = self._respond_flow

    def _respond_flow(self, request: dict[str, object]) -> dict[str, object]:
        payload = context_payload(request)
        if "exploration_direction" in payload:
            return self._respond_child(request, payload)
        if "visual_outline" not in payload:
            return self.call("submit_visual_outline", outline_arguments())
        integration = payload["integration_materials"]
        if integration["comparison"] is not None:
            return self.call("submit_integration", {"preserve": True})
        if integration["progress"]["works"]:
            return self.call("finish_analysis", {})
        return self._read_comparison(integration["catalog"]["entries"])

    def _read_comparison(self, rows: list[dict[str, object]]) -> dict[str, object]:
        libraries = ("feature_library", "sketch_library")
        requests = [{"library": library, "ids": [row["id"] for row in rows if row["library"] == library]} for library in libraries]
        requests = [request for request in requests if request["ids"]]
        targets = [identity for request in requests for identity in request["ids"]]
        return self.call("read_library", {"comparison": {"question": "比较组成与外观是否重复", "target_ids": targets}, "requests": requests})

    def _respond_child(self, request: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
        direction = str(payload["exploration_direction"])
        self.assertEqual(set(payload), {"user_request", "visual_outline", "exploration_direction"})
        self.assertNotIn("独有机制-", json.dumps(request, ensure_ascii=False))
        self.assertNotIn("未被草图引用-", json.dumps(request, ensure_ascii=False))
        if direction == self.stopped_direction:
            return self.call("stop_exploration", {"reason": "目前无法形成可信组成, 需要更多视觉信息"})
        return self.call("submit_exploration", report_arguments(direction)["report"])

    def _run(self, **options: object) -> AnalysisOutcome:
        return run_analysis(self.root / "reference.PNG", "探索条带形成方式", options=replace(self.analysis_options, **options))

    def _snapshot(self, outcome: AnalysisOutcome) -> dict[str, object]:
        return json.loads((outcome.run_dir / "run.json").read_text())

    def test_index_read_submit_finish_preserves_unread_candidate_bodies(self) -> None:
        outcome = self._run()
        snapshot = self._snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "completed", snapshot)
        library = outcome.summary_result.analysis_detail
        self.assertIsInstance(library, PossibilityLibrary)
        self.assertEqual([item.id for item in library.elements], ["E1"])
        self.assertEqual(len(library.sketch_library), 2)
        self.assertEqual(len(library.feature_library), 2)
        mechanisms = {candidate.mechanism for item in library.feature_library for candidate in item.candidates}
        self.assertEqual(mechanisms, {"独有机制-平面组织", "独有机制-空间形态", "未被草图引用-平面组织", "未被草图引用-空间形态"})
        children = [item for item in outcome.state["results"].values() if isinstance(item.analysis_detail, ExplorationReport)]
        self.assertEqual(len(children), 2)
        main_requests = [request for request in self.requests if "exploration_direction" not in context_payload(request)]
        self.assertEqual(snapshot["main_execution"]["model_calls"], 4)
        self.assertEqual(len(main_requests), 4)
        self.assertTrue(all("独有机制-" not in json.dumps(request, ensure_ascii=False) for request in main_requests))
        self.assertIsNone(context_payload(main_requests[1])["integration_materials"]["comparison"])
        selected = context_payload(main_requests[2])["integration_materials"]["comparison"]["entries"]
        self.assertEqual(len(selected), 4)
        for request in self.requests:
            payload = context_payload(request)
            expected = EXPLORATION_PROMPT if "exploration_direction" in payload else INTEGRATION_PROMPT
            if "visual_outline" not in payload:
                expected = OUTLINE_PROMPT
            system = "\n".join(message["content"] for message in request["messages"] if message["role"] == "system")
            self.assertEqual(system, expected)
            advertised = {item["function"]["name"] for item in request["tools"]}
            self.assertFalse(advertised & {"task", "read_file", "write_file", "read_library_item", "run_analysis_batch"})
            if "integration_materials" in payload:
                self.assertTrue({"list_library", "read_library", "finish_analysis"}.issubset(advertised))
        self.assertEqual(len(snapshot["presented_library_items"]), 4)

    def test_outline_repairs_do_not_consume_integration_work_allowance(self) -> None:
        outline_calls = 0
        outline_failures = 2
        integration_rejected = False

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal outline_calls, integration_rejected
            payload = context_payload(request)
            if "visual_outline" not in payload:
                outline_calls += 1
                arguments = outline_arguments()
                if outline_calls <= outline_failures:
                    arguments["outline"]["elements"][0]["unexpected"] = True
                return self.call("submit_visual_outline", arguments)
            if "integration_materials" in payload and payload["integration_materials"]["comparison"] is not None:
                if not integration_rejected:
                    integration_rejected = True
                    target = payload["integration_materials"]["comparison"]["target_ids"][0]
                    return self.call("submit_integration", {"merges": [{"kind": "feature", "members": [target]}]})
                draft = control_payload(request)["submission"]
                return self.call(
                    "repair_analysis_submission",
                    {
                        "draft_id": draft["draft_id"],
                        "expected_revision": draft["revision"],
                        "changes": [{"op": "remove", "path": "/merges"}, {"op": "set", "path": "/preserve", "value": True}],
                    },
                )
            return self._respond_flow(request)

        self.response = respond
        outcome = self._run(outline_max_output_tokens=4096, integration_max_output_tokens=8192, worker_max_output_tokens=12288)
        snapshot = self._snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "completed", snapshot)
        self.assertEqual(snapshot["main_execution"]["format_repair_scopes"], {"outline": 2, "comparison-1": 1})
        self.assertEqual(snapshot["main_execution"]["format_repair_calls"], 3)
        for request in self.requests:
            payload = context_payload(request)
            expected = 12288 if "exploration_direction" in payload else 4096 if "visual_outline" not in payload else 8192
            self.assertEqual(request["max_completion_tokens"], expected)
        records = [json.loads(line) for line in (outcome.run_dir / "tools.jsonl").read_text().splitlines()]
        self.assertTrue(any(record["outcome"] == "rejected" for record in records))
        self.assertTrue(any(record["tool"] == "repair_analysis_submission" and record["outcome"] == "success" for record in records))

    def test_batch_merges_and_local_repair_preserve_history_and_candidates(self) -> None:
        integration_calls = 0

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal integration_calls
            payload = context_payload(request)
            if "integration_materials" not in payload:
                return self._respond_flow(request)
            if payload["integration_materials"]["comparison"] is None:
                return self._respond_flow(request)
            integration_calls += 1
            entries = payload["integration_materials"]["comparison"]["entries"]
            features = [item["handle"] for item in entries if item["kind"] == "feature"]
            sketches = [item["handle"] for item in entries if item["kind"] == "sketch"]
            if integration_calls == 1:
                return self.call(
                    "submit_integration", {"merges": [{"kind": "feature", "members": [*features, "bad"]}, {"kind": "sketch", "members": sketches}]}
                )
            rejected = control_payload(request)["submission"]
            self.assertEqual(rejected["errors"][0]["path"], "/merges/0/members/2")
            self.assertTrue(any(message.get("role") == "assistant" for message in request["messages"]))
            return self.call(
                "repair_analysis_submission",
                {
                    "draft_id": rejected["draft_id"],
                    "expected_revision": rejected["revision"],
                    "changes": [{"op": "remove", "path": "/merges/0/members/2"}],
                },
            )

        self.response = respond
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "completed", self._snapshot(outcome))
        self.assertEqual(integration_calls, 2)
        library = outcome.summary_result.analysis_detail
        self.assertEqual(len(library.feature_library), 1)
        self.assertEqual(len(library.sketch_library), 1)
        self.assertEqual(len(library.feature_library[0].candidates), 4)

    def test_two_groups_keep_repair_history_locally_and_release_previous_bodies(self) -> None:
        first_owner = None
        first_mechanisms = []
        rejected_once = False
        second_seen = False
        expected_groups = 2

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal first_owner, first_mechanisms, rejected_once, second_seen
            payload = context_payload(request)
            if "integration_materials" not in payload:
                return self._respond_flow(request)
            integration = payload["integration_materials"]
            comparison = integration["comparison"]
            works = integration["progress"]["works"]
            if comparison is None:
                for mechanism in first_mechanisms:
                    self.assertNotIn(mechanism, json.dumps(request, ensure_ascii=False))
                self.assertFalse(tool_results(request))
                if len(works) == expected_groups:
                    return self.call("finish_analysis", {})
                owners = [row["id"] for row in integration["catalog"]["entries"] if row["library"] == "feature_library"]
                owner = owners[0] if not works else next(identifier for identifier in owners if identifier != first_owner)
                if first_owner is None:
                    first_owner = owner
                return self.call(
                    "read_library",
                    {
                        "comparison": {"question": "检查当前外观的机制", "target_ids": [owner]},
                        "requests": [{"library": "feature_library", "ids": [owner], "include_candidates": True}],
                    },
                )
            if comparison["target_ids"] == [first_owner]:
                first_mechanisms = [entry["content"]["mechanism"] for entry in comparison["entries"] if entry["kind"] == "candidate"]
                if not rejected_once:
                    rejected_once = True
                    return self.call("submit_integration", {"merges": [{"kind": "feature", "members": [first_owner, "missing"]}]})
                self.assertTrue(control_payload(request)["submission"]["errors"])
                self.assertTrue(any(message["role"] == "assistant" for message in request["messages"]))
                self.assertEqual(len(first_mechanisms), 2)
            else:
                second_seen = True
                for mechanism in first_mechanisms:
                    self.assertNotIn(mechanism, json.dumps(request, ensure_ascii=False))
                self.assertFalse(any(result.get("status") == "invalid_submission" for result in tool_results(request)))
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "completed", self._snapshot(outcome))
        self.assertTrue(second_seen)
        self.assertEqual(len(self._snapshot(outcome)["integration"]["works"]), 2)
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 2)

    def test_same_response_reads_accumulate_but_cannot_authorize_immediate_submit(self) -> None:
        selected_owners = []
        received_bodies = False

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal selected_owners, received_bodies
            payload = context_payload(request)
            if "integration_materials" not in payload:
                return self._respond_flow(request)
            integration = payload["integration_materials"]
            comparison = integration["comparison"]
            if comparison is None and not selected_owners:
                selected_owners = [row["id"] for row in integration["catalog"]["entries"] if row["library"] == "feature_library"]
                combined = self.call(
                    "read_library",
                    {
                        "comparison": {"question": "比较两个拥有者的候选机制", "target_ids": [selected_owners[0]]},
                        "requests": [{"library": "feature_library", "ids": [selected_owners[0]], "include_candidates": True}],
                    },
                )
                second = self.call(
                    "read_library",
                    {
                        "extend_target_ids": [selected_owners[1]],
                        "requests": [{"library": "feature_library", "ids": [selected_owners[1]], "include_candidates": True}],
                    },
                )
                second["tool_calls"][0]["id"] = "second-library-read"
                premature = self.call("submit_integration", {"preserve": True})
                premature["tool_calls"][0]["id"] = "premature-preserve"
                combined["tool_calls"].extend(second["tool_calls"] + premature["tool_calls"])
                return combined
            if comparison is None:
                return self.call("finish_analysis", {})
            # 同一响应中的工具执行顺序不保证; 补读若早于建组被拒绝, 下一请求明确重试.
            owners = [entry["handle"] for entry in comparison["entries"] if entry["kind"] == "feature"]
            if selected_owners[1] not in owners:
                return self.call(
                    "read_library",
                    {
                        "extend_target_ids": [selected_owners[1]],
                        "requests": [{"library": "feature_library", "ids": [selected_owners[1]], "include_candidates": True}],
                    },
                )
            received_bodies = True
            self.assertCountEqual(owners, selected_owners)
            self.assertEqual(len([entry for entry in comparison["entries"] if entry["kind"] == "candidate"]), 4)
            receipts = tool_results(request)
            selected_ids = [identity for receipt in receipts if receipt.get("status") == "selected" for identity in receipt["selected_ids"]]
            self.assertTrue(set(selected_owners).issubset(selected_ids))
            rejected = control_payload(request)["submission"]
            self.assertTrue(rejected["errors"])
            self.assertNotEqual(rejected.get("status"), "submitted")
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "completed", self._snapshot(outcome))
        self.assertTrue(received_bodies)
        self.assertEqual(len(self._snapshot(outcome)["integration"]["works"]), 1)

    def test_stopped_child_is_retained_and_library_partial(self) -> None:
        self.stopped_direction = "空间形态"
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "partial", self._snapshot(outcome))
        self.assertEqual(outcome.summary_result.status, "partial")
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 1)
        children = [item for item in outcome.state["results"].values() if item.task_id != outcome.task_id]
        self.assertEqual(sorted(item.status for item in children), ["blocked", "completed"])

    def test_failed_outline_issue_is_presented_and_deferred(self) -> None:
        issued = False

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal issued
            payload = context_payload(request)
            if payload.get("exploration_direction") == "空间形态":
                if not issued:
                    issued = True
                    return self.call("report_outline_issue", {"region": "角落", "description": "遗漏对象无法引用", "element_ids": []})
                return self.call("stop_exploration", {"reason": "缺少元素身份"})
            if "integration_materials" in payload and payload["integration_materials"]["comparison"] is not None:
                issues = payload["integration_materials"]["context"]["outline_issues"]
                self.assertEqual(len(issues), 1)
                return self.call(
                    "submit_integration",
                    {
                        "preserve": True,
                        "outline_issue_decisions": [{"issue_id": issues[0]["id"], "disposition": "deferred", "reason": "下一轮修订元素"}],
                    },
                )
            return self._respond_flow(request)

        self.response = respond
        outcome = self._run()
        snapshot = self._snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "partial", snapshot)
        self.assertEqual(len(snapshot["outline_versions"]), 1)
        self.assertEqual(snapshot["issue_resolutions"]["1"]["disposition"], "deferred")

    def test_uncovered_element_is_recorded_without_fabricated_candidate(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if "visual_outline" not in context_payload(request):
                arguments = outline_arguments()
                arguments["outline"]["elements"].append({"id": "E2", "name": "背景", "scope": "全图", "salient_features": ["暗部"]})
                return self.call("submit_visual_outline", arguments)
            return self._respond_flow(request)

        self.response = respond
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "partial", self._snapshot(outcome))
        self.assertIn({"reason": "uncovered_elements", "element_ids": ["E2"]}, self._snapshot(outcome)["gaps"])

    def test_multiple_crops_are_presented_together_before_decision(self) -> None:
        with Image.new("RGB", (8, 6), (30, 80, 120)) as reference:
            reference.save(self.root / "reference.PNG")
        measured = False

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal measured
            payload = context_payload(request)
            if "integration_materials" not in payload or payload["integration_materials"]["comparison"] is None:
                return self._respond_flow(request)
            if not measured:
                measured = True
                return self.call(
                    "measure_reference",
                    {
                        "requests": [
                            {"question": "查看边缘", "measurement": {"kind": "crop", "region": region}}
                            for region in [{"left": 1, "top": 1, "right": 4, "bottom": 3}, {"left": 4, "top": 1, "right": 7, "bottom": 4}]
                        ]
                    },
                )
            images = [
                block["image_url"]["url"]
                for message in request["messages"]
                if isinstance(message["content"], list)
                for block in message["content"]
                if block.get("type") == "image_url"
            ]
            self.assertEqual(len(images), 3)
            for url, size in zip(images[1:], [(3, 2), (3, 3)], strict=True):
                with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as crop:
                    self.assertEqual(crop.size, size)
            self.assertNotIn("measure_reference", {item["function"]["name"] for item in request["tools"]})
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        outcome = self._run(max_measurements=2)
        self.assertEqual(outcome.stop_reason, "completed", self._snapshot(outcome))
        self.assertEqual(len(outcome.state["measurements"]), 2)
        self.assertEqual(self._snapshot(outcome)["main_execution"]["model_calls"], 5)

    def test_package_call_limit_preserves_partial_result(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if "integration_materials" in payload and payload["integration_materials"]["comparison"] is not None:
                return {"role": "assistant", "content": "还在分析"}
            return self._respond_flow(request)

        self.response = respond
        outcome = self._run(max_integration_calls=2)
        self.assertEqual(outcome.stop_reason, "partial", self._snapshot(outcome))
        self.assertEqual(self._snapshot(outcome)["main_execution"]["model_calls"], 3)
        self.assertTrue(self._snapshot(outcome)["integration"]["deferred"])

    def test_deferred_comparison_is_not_completed(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if "integration_materials" in payload and payload["integration_materials"]["comparison"] is not None:
                return self.call("submit_integration", {"preserve": True, "deferred_work": ["跨组成的遮挡比较尚未完成"]})
            return self._respond_flow(request)

        self.response = respond
        outcome = self._run()
        self.assertEqual(outcome.stop_reason, "partial", self._snapshot(outcome))
        self.assertTrue(self._snapshot(outcome)["integration"]["deferred"])

    def test_main_can_stop_without_fabricating_outline_or_results(self) -> None:
        self.response = lambda _request: self.call("stop_analysis", {"reason": "无法可靠辨认图中元素, 需要更清晰参考图"})
        outcome = self._run(max_main_calls=0)
        snapshot = self._snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "stopped", snapshot)
        self.assertIsNone(outcome.summary_result)
        self.assertIsNone(snapshot["visual_outline"])
        self.assertEqual(snapshot["worker_executions"], {})
        self.assertEqual(len(self.requests), 1)
        self.assertIn("更清晰参考图", snapshot["main_execution"]["error"])

    def test_stop_rejects_later_outline_submission_in_the_same_response(self) -> None:
        original_stop = ExplorationSession._stop_tool

        def slow_stop_tool(session: ExplorationSession) -> StructuredTool:
            stop = cast("StructuredTool", original_stop(session))
            self.assertIsNotNone(stop.func)
            callback = cast("Callable[[str], str]", stop.func)

            def delayed_stop(reason: str) -> str:
                # 放大后续工具抢先执行的机会, 验证真实状态而非仅检查配置值.
                time.sleep(0.03)
                return callback(reason)

            stop.func = delayed_stop
            return stop

        def stop_then_submit(_request: dict[str, object]) -> dict[str, object]:
            response = self.call("stop_analysis", {"reason": "参考图无法辨认, 停止分析"})
            submission = self.call("submit_visual_outline", outline_arguments())
            submission["tool_calls"][0]["id"] = "outline-after-stop"
            response["tool_calls"].extend(submission["tool_calls"])
            return response

        self.response = stop_then_submit
        with patch.object(ExplorationSession, "_stop_tool", slow_stop_tool):
            outcome = self._run(max_main_calls=1)
        snapshot = self._snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "stopped", snapshot)
        self.assertEqual(snapshot["main_execution"]["status"], "stopped")
        self.assertIsNone(outcome.summary_result)
        self.assertIsNone(snapshot["visual_outline"])
        self.assertEqual(snapshot["outline_versions"], [])
        self.assertEqual(snapshot["worker_executions"], {})
        self.assertFalse((outcome.run_dir / "submissions" / "A1-outline.json").exists())
        self.assertFalse((outcome.run_dir / "possibility_library").exists())
        self.assertEqual(len(self.requests), 1)

    def test_historical_input_requires_explicit_conversion_before_any_model_request(self) -> None:
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="分析", reference_path=str(self.root / "reference.PNG")))
        state = add_task(state, TaskRecord(id="old-root", role="analysis", target_version="T1", objective="旧流程"))
        state = add_result(state, ResultRecord(id="legacy", task_id="old-root", status="blocked", summary="旧结果"))
        state = add_task(state, TaskRecord(id="new-root", role="analysis", target_version="T1", objective="新流程", related_result_ids=("legacy",)))
        with self.assertRaisesRegex(ValueError, "explicit conversion"):
            run_analysis_task(state, "new-root", options=self.analysis_options)
        self.assertEqual(self.requests, [])

    def test_cli_emits_actual_new_library_and_honors_yaml_transport_settings(self) -> None:
        config = self.root / "analysis.yaml"
        config.write_text("max_output_tokens: 2048\nmax_worker_calls: 4\nstream_model_responses: false\noutput_dir: analysis\n", encoding="utf-8")
        output, errors = StringIO(), StringIO()
        arguments = ["shader-deep-analyze", str(self.root / "reference.PNG"), "探索条带形成方式", "--config", str(config)]
        with patch("sys.argv", arguments), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(), 0)
        payload = json.loads(output.getvalue())
        run = Path(payload["run_dir"])
        snapshot = json.loads((run / "run.json").read_text())
        actual = payload["result"]
        self.assertEqual(actual["analysis_protocol"], "possibility_library_v1")
        self.assertEqual(set(actual["analysis_detail"]), {"elements", "sketch_library", "feature_library", "relation_library"})
        # CLI 省略可选空值, 回读成同一契约后必须与实际保存的结果完全一致.
        actual["analysis_detail"] = json.loads(json.dumps(asdict(TypeAdapter(PossibilityLibrary).validate_python(actual["analysis_detail"]))))
        self.assertEqual(actual, snapshot["blackboard"]["results"][snapshot["summary_result_id"]])
        self.assertIn("Status: completed", errors.getvalue())
        self.assertEqual(run.parent, self.root / "analysis")
        self.assertEqual(snapshot["options"]["max_output_tokens"], 2048)
        self.assertEqual(snapshot["options"]["max_worker_calls"], 4)
        self.assertEqual({request["max_completion_tokens"] for request in self.requests}, {2048})
        self.assertTrue(all(request["stream"] is False for request in self.requests))
