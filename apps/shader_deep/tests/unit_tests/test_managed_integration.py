"""实际默认入口按本轮身份发现、执行和复核, 不用旧样本固定 ID."""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

from pydantic import TypeAdapter

from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.exploration import VisualOutline
from shader_deep.analysis.replay import run_replay
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from shader_deep.analysis_cli import main
from tests.unit_tests._generation_fixture import GenerationFixture, tool_results
from tests.unit_tests.test_exploration_flow import outline_arguments, report_arguments


def payload_of(request: dict[str, object]) -> dict[str, object]:
    for message in reversed(request["messages"]):
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "text" and block["text"].startswith("{"):
                payload = json.loads(block["text"])
                if any(key in payload for key in ("user_request", "comparison", "kind")):
                    return payload
    msg = "Missing current analysis input"
    raise AssertionError(msg)


class ManagedIntegrationTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.options = AnalysisOptions(output_dir=self.root / "managed", max_main_calls=40, max_integration_calls=3, max_parallel=1)
        self.report_issue = False
        self.confirm_revision = True
        self.reject_feature = False
        self.group_feature_names = False
        self.response = self.respond_managed

    def respond_managed(self, request: dict[str, object]) -> dict[str, object]:
        payload = payload_of(request)
        if "exploration_direction" in payload:
            return self.respond_explorer(request, payload)
        kind = payload.get("kind")
        if kind == "outline-review":
            return self.call(
                "review_outline",
                {
                    "decisions": [{"issue_id": "1", "disposition": "revised", "reason": "参考图可见柔和亮边"}],
                    "element_features": {"E1": ["柔和亮边"]},
                },
            )
        if kind == "outline-verify":
            self.assertEqual(payload["previous_outline"]["elements"][0]["salient_features"], ["边缘较亮"])
            self.assertEqual(payload["visual_outline"]["elements"][0]["salient_features"], ["柔和亮边"])
            self.assertIn("image_url", json.dumps(request["messages"]))
            return self.call("verify_outline", {"decisions": [{"issue_id": "1", "confirmed": self.confirm_revision, "reason": "已核对原图与文字"}]})
        if kind in {"discover-owners", "discover-candidates"}:
            return self.plan(payload)
        if payload.get("comparison"):
            return self.compare(request, payload)
        return self.call("submit_visual_outline", outline_arguments())

    def respond_explorer(self, request: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
        if self.report_issue and payload["exploration_direction"] == "平面组织" and not tool_results(request):
            return self.call("report_outline_issue", {"region": "条带", "description": "初稿遗漏柔和过渡", "element_ids": ["E1"]})
        return self.call("submit_exploration", report_arguments(payload["exploration_direction"])["report"])

    def plan(self, payload: dict[str, object]) -> dict[str, object]:
        kind = payload["kind"]
        grouped = defaultdict(list)
        for entry in payload["entries"]:
            if kind == "discover-owners":
                key = (entry["kind"], entry["content"]["name"] if self.group_feature_names else "")
                grouped[key].append(entry["handle"])
            elif entry["kind"] == "candidate":
                grouped[entry["owner"]].append(entry["handle"])
        groups = [
            {"kind": "candidate" if kind == "discover-candidates" else key[0], "target_ids": identities, "question": "比较当前相同范围的描述"}
            for key, identities in grouped.items()
            if len(identities) > 1
        ]
        return self.call("plan_comparisons", {"comparisons": groups, "reason": "存在同对象的跨报告描述"})

    def compare(self, request: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
        comparison = payload["comparison"]
        target = next(entry for entry in comparison["entries"] if entry["handle"] == comparison["target_ids"][0])
        self.assertNotIn("image_url", json.dumps(request["messages"]))
        self.assertGreater(payload["budget"]["work_calls_remaining"], 0)
        self.assertEqual({t["function"]["name"] for t in request["tools"]}, {"submit_integration", "repair_analysis_submission"})
        if target["kind"] == "feature":
            if self.reject_feature:
                return self.call("submit_integration", {"unresolved": {"E1": ["不允许越权"]}})
            return self.call("submit_integration", {"merges": [{"kind": "feature", "members": comparison["target_ids"]}]})
        return self.call("submit_integration", {"preserve": True})

    def run_default(self) -> AnalysisOutcome:
        return run_analysis(self.root / "reference.PNG", "比较外观并保留不同机制", options=self.options)

    @staticmethod
    def snapshot(outcome: AnalysisOutcome) -> dict[str, object]:
        return json.loads((outcome.run_dir / "run.json").read_text())

    def test_default_entry_discovers_new_ids_and_keeps_all_candidate_mechanisms(self) -> None:
        outcome = self.run_default()
        self.assertEqual(outcome.stop_reason, "completed", self.snapshot(outcome))
        library = outcome.summary_result.analysis_detail
        self.assertEqual(len(library.feature_library), 1)
        self.assertEqual(len(library.feature_library[0].candidates), 4)
        rows = json.loads((outcome.run_dir / "integration-work.json").read_text())
        self.assertTrue(any(row["status"] == "modified" for row in rows))
        self.assertTrue(any(row["status"] == "preserved" for row in rows))
        self.assertNotIn("run-avu0ozrm", json.dumps(self.requests))

    def test_more_than_six_groups_run_when_existing_global_budgets_allow(self) -> None:
        self.group_feature_names = True
        feature_count = 7

        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = payload_of(request)
            if "exploration_direction" in payload:
                report = report_arguments(payload["exploration_direction"])["report"]
                original = report["feature_library"][0]
                for index in range(2, feature_count + 1):
                    clone = deepcopy(original)
                    clone.update(id=f"F{index}", name=f"亮边-{index}")
                    for offset, candidate in enumerate(clone["candidates"]):
                        candidate["id"] = f"C{index}-{offset}"
                    report["feature_library"].append(clone)
                return self.call("submit_exploration", report)
            if payload.get("kind") == "discover-candidates":
                self.assertEqual(len(payload["owner_comparisons"]), feature_count + 1)
            return self.respond_managed(request)

        self.response = respond
        outcome = self.run_default()
        self.assertEqual(outcome.stop_reason, "completed", self.snapshot(outcome))
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), feature_count)
        rows = json.loads((outcome.run_dir / "integration-work.json").read_text())
        self.assertEqual(sum(row["status"] == "modified" for row in rows), feature_count)

    def test_subagent_isolates_outline_history_and_records_its_own_calls(self) -> None:
        marker = "仅属于初稿会话的临时推理"

        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = payload_of(request)
            reply = self.respond_managed(request)
            if not payload.get("kind") and not payload.get("comparison") and "exploration_direction" not in payload:
                reply["content"] = marker
            else:
                self.assertNotIn(marker, json.dumps(request["messages"], ensure_ascii=False))
            return reply

        self.response = respond
        outcome = self.run_default()
        saved = self.snapshot(outcome)
        child = json.loads((outcome.run_dir / "integration-subagent.json").read_text())
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(saved["coordinator_execution"]["model_calls"], 1)
        self.assertEqual(saved["integration_execution"], child["execution"])
        self.assertEqual(saved["main_execution"]["model_calls"], 1 + child["execution"]["model_calls"])
        self.assertEqual(child["parent_task_id"], outcome.task_id)
        self.assertNotEqual(child["task_id"], outcome.task_id)
        events = [json.loads(line) for line in (outcome.run_dir / "events.jsonl").read_text().splitlines()]
        integration_requests = [event for event in events if event["role"] == "integration" and event["event"] == "model_started"]
        self.assertEqual(len(integration_requests), child["execution"]["model_calls"])
        self.assertEqual({event["task_id"] for event in integration_requests}, {child["task_id"]})
        self.assertGreater(child["execution"]["model_calls"], 0)

    def test_exhausted_parent_budget_does_not_become_unlimited_in_subagent(self) -> None:
        self.options = replace(self.options, max_main_calls=1)
        outcome = self.run_default()
        saved = self.snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(saved["main_execution"]["model_calls"], 1)
        self.assertEqual(saved["integration_execution"]["model_calls"], 0)
        self.assertTrue(all(not payload_of(request).get("comparison") and not payload_of(request).get("kind") for request in self.requests))
        self.assertTrue(any(gap["reason"] == "global_call_budget_exhausted" for gap in saved["gaps"]))
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 2)

    def test_running_snapshot_tracks_subagent_comparisons_and_outline_revision(self) -> None:
        self.report_issue = True
        checkpoints = []

        def respond(request: dict[str, object]) -> dict[str, object]:
            if payload_of(request).get("kind") == "discover-candidates":
                directory = next(self.options.output_dir.glob("run-*"))
                saved = json.loads((directory / "run.json").read_text())
                child = json.loads((directory / "integration-subagent.json").read_text())
                self.assertIsNone(saved["summary_result_id"])
                self.assertEqual(saved["stop_reason"], "running")
                self.assertEqual(saved["main_execution"]["status"], "running")
                self.assertGreater(saved["integration_execution"]["model_calls"], 0)
                self.assertEqual(saved["integration_execution"], child["execution"])
                self.assertEqual(saved["main_execution"]["model_calls"], 1 + child["execution"]["model_calls"])
                self.assertTrue(saved["integration"]["works"])
                self.assertEqual(saved["integration"], child["integration"])
                outlines = TypeAdapter(list[VisualOutline])
                self.assertEqual(outlines.validate_python(saved["outline_versions"]), outlines.validate_python(child["outline_versions"]))
                self.assertEqual(len(saved["outline_versions"]), 2)
                self.assertEqual(saved["issue_resolutions"]["1"]["disposition"], "revised")
                self.assertEqual(saved["phase_budgets"]["integration"], child["phase_budgets"]["integration"])
                checkpoints.append(saved)
            return self.respond_managed(request)

        self.response = respond
        outcome = self.run_default()
        self.assertEqual(outcome.stop_reason, "completed", self.snapshot(outcome))
        self.assertEqual(len(checkpoints), 1)

    def test_local_exhaustion_keeps_deferred_work_and_continues_independent_work(self) -> None:
        self.reject_feature = True
        self.options = replace(self.options, max_integration_calls=2)
        outcome = self.run_default()
        self.assertEqual(outcome.stop_reason, "partial")
        rows = json.loads((outcome.run_dir / "integration-work.json").read_text())
        deferred = [row for row in rows if row["status"] == "deferred" and row.get("comparison_id")]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["model_calls"], 2)
        self.assertTrue(any(row["status"] == "preserved" for row in rows))
        self.assertEqual(outcome.summary_result.analysis_detail.elements[0].unresolved, ())
        saved = self.snapshot(outcome)
        child = json.loads((outcome.run_dir / "integration-subagent.json").read_text())
        self.assertEqual(saved["integration"], child["integration"])
        self.assertEqual(saved["gaps"], child["gaps"])

    def test_global_limit_preserves_published_result_without_resetting_calls(self) -> None:
        self.options = replace(self.options, max_main_calls=3)
        outcome = self.run_default()
        state = self.snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(state["main_execution"]["model_calls"], 3)
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 1)
        self.assertTrue(any(gap["reason"] == "global_call_budget_exhausted" for gap in state["gaps"]))
        child = json.loads((outcome.run_dir / "integration-subagent.json").read_text())
        self.assertEqual(state["gaps"], child["gaps"])

    def test_revision_requires_separate_presented_verification_and_preserves_original_reports(self) -> None:
        self.report_issue = True
        outcome = self.run_default()
        state = self.snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "completed", state)
        self.assertEqual(len(state["outline_versions"]), 2)
        self.assertEqual(state["issue_resolutions"]["1"]["disposition"], "revised")
        self.assertEqual(state["issue_resolutions"]["1"]["verified_outline_version"], 2)
        workers = [task for task in state["blackboard"]["tasks"].values() if task["parent_task_id"]]
        original = TypeAdapter(VisualOutline).validate_python(state["outline_versions"][0])
        self.assertTrue(all(TypeAdapter(VisualOutline).validate_python(task["analysis_outline"]) == original for task in workers))

    def test_unconfirmed_revision_keeps_issue_partial(self) -> None:
        self.report_issue, self.confirm_revision = True, False
        outcome = self.run_default()
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(self.snapshot(outcome)["issue_resolutions"]["1"]["disposition"], "deferred")

    def test_revision_without_verification_budget_cannot_close_issue(self) -> None:
        self.report_issue = True
        self.options = replace(self.options, max_main_calls=2)
        outcome = self.run_default()
        state = self.snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(len(state["outline_versions"]), 2)
        self.assertEqual(state["issue_resolutions"]["1"]["disposition"], "verification_pending")

    def test_outline_identity_replacement_is_rejected_while_independent_work_continues(self) -> None:
        self.report_issue = True

        def respond(request: dict[str, object]) -> dict[str, object]:
            if payload_of(request).get("kind") == "outline-review":
                return self.call(
                    "review_outline",
                    {"decisions": [{"issue_id": "1", "disposition": "revised", "reason": "尝试越界"}], "outline": {"elements": [{"id": "new"}]}},
                )
            return self.respond_managed(request)

        self.response = respond
        outcome = self.run_default()
        state = self.snapshot(outcome)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(len(state["outline_versions"]), 1)
        self.assertEqual(state["issue_resolutions"], {})
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 1)

    def test_replay_uses_same_managed_path_without_reexploration(self) -> None:
        original = self.run_default()
        self.requests.clear()
        outcome = run_replay(original.run_dir, options=replace(self.options, output_dir=self.root / "replay"))
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertTrue(all("exploration_direction" not in payload_of(request) for request in self.requests))
        self.assertEqual(len(outcome.summary_result.analysis_detail.feature_library), 1)
        saved = self.snapshot(outcome)
        self.assertEqual(saved["coordinator_execution"]["model_calls"], 0)
        self.assertEqual(saved["integration_execution"]["model_calls"], len(self.requests))

    def test_cli_default_uses_managed_flow_and_honors_explicit_transport_budget(self) -> None:
        output, errors = StringIO(), StringIO()
        arguments = [
            "shader-deep-analyze",
            str(self.root / "reference.PNG"),
            "比较外观",
            "--output-dir",
            str(self.root / "cli"),
            "--max-output-tokens",
            "4096",
            "--no-stream-model-responses",
        ]
        with patch("sys.argv", arguments), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")
        self.assertTrue(any(payload_of(request).get("kind") == "discover-owners" for request in self.requests))
        self.assertEqual({request["max_completion_tokens"] for request in self.requests}, {4096})
        self.assertTrue(all(not request["stream"] for request in self.requests))
