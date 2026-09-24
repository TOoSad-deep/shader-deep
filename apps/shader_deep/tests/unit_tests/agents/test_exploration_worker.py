"""检查真实模型请求的输入隔离, 三库提交及局部恢复路径."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.agents.exploration.agent import run_exploration
from shader_deep.agents.exploration.context import build_exploration_context
from shader_deep.agents.outline.context import build_outline_context
from shader_deep.compatibility.ondemand.context import build_integration_context
from shader_deep.domain.library.models import ExplorationReport, OutlineElement, VisualOutline
from shader_deep.infrastructure.tracing import EventLog
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results

if TYPE_CHECKING:
    from collections.abc import Callable

    from shader_deep.agents.exploration.contracts import ExplorationOutcome
    from shader_deep.runtime.runner import AnalysisLoop

REFERENCE = "data:image/png;base64,original-image-bytes"
REPORT = {
    "sketch_library": [{"id": "S1", "name": "平面组成", "element_ids": ["E1"], "composition": ["均匀色面"]}],
    "feature_library": [],
    "relation_library": [],
}


def request_context(request: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    for message in reversed(request["messages"]):
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "text" and block["text"].startswith('{"user_request":'):
                return json.loads(block["text"]), content
    msg = "No actual exploration context in model request"
    raise AssertionError(msg)


class ExplorationWorkerTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.outline = VisualOutline(elements=(OutlineElement(id="E1", name="背景", scope="全图", salient_features=("均匀深色",)),), relations=())
        self.worker_options = AnalysisOptions(max_worker_calls=5, max_request_retries=0)

    def run_worker(
        self,
        task_id: str = "worker-1",
        direction: str = "探索平面形成方式",
        *,
        commit_report: Callable[[ExplorationReport], dict[str, object]] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ExplorationOutcome:
        return run_exploration(
            "理解参考图",
            self.outline,
            direction,
            REFERENCE,
            options=self.worker_options,
            config={"recursion_limit": 50},
            directory=self.root,
            task_id=task_id,
            event_log=EventLog(self.root),
            commit_report=commit_report,
            should_stop=should_stop,
        )

    def test_stage_contexts_keep_business_fields_and_actual_image_separate(self) -> None:
        outline = build_outline_context("原始要求", REFERENCE)
        self.assertEqual(json.loads(outline.content[0]["text"]), {"user_request": "原始要求"})
        child = build_exploration_context("原始要求", self.outline, "新的开放问题", REFERENCE, control={"submission": {"revision": 2}})
        self.assertEqual(set(json.loads(child.content[0]["text"])), {"user_request", "visual_outline", "exploration_direction"})
        self.assertEqual(child.content[1], {"type": "image_url", "image_url": {"url": REFERENCE}})
        self.assertIn("运行控制反馈", child.content[2]["text"])
        main = build_integration_context("原始要求", self.outline, REFERENCE, index={"items": 3})
        self.assertEqual(json.loads(main.content[0]["text"])["integration_materials"], {"items": 3})
        self.assertEqual(main.content[1], child.content[1])

    def test_valid_report_needs_no_summary_and_child_histories_are_independent(self) -> None:
        self.response = lambda _: self.call("submit_exploration", REPORT)
        first = self.run_worker(direction="第一轮独立方向")
        second = self.run_worker(task_id="worker-2", direction="第二轮独立方向")
        self.assertEqual(first.execution.status, "completed")
        self.assertEqual(second.execution.status, "completed")
        self.assertEqual(first.report.sketch_library[0].composition, ("均匀色面",))
        self.assertEqual(len(self.requests), 2)
        for index, request in enumerate(self.requests):
            payload, blocks = request_context(request)
            self.assertEqual(set(payload), {"user_request", "visual_outline", "exploration_direction"})
            self.assertEqual(payload["exploration_direction"], ["第一轮独立方向", "第二轮独立方向"][index])
            self.assertIn({"type": "image_url", "image_url": {"url": REFERENCE}}, blocks)
            self.assertFalse(tool_results(request))
        self.assertNotIn("第一轮独立方向", json.dumps(self.requests[1], ensure_ascii=False))

    def test_invalid_reference_is_repaired_without_rewriting_composition(self) -> None:
        invalid = deepcopy(REPORT)
        invalid["sketch_library"][0]["element_ids"] = ["missing"]

        def respond(request: dict[str, object]) -> dict[str, object]:
            replies = tool_results(request)
            if not replies:
                return self.call("submit_exploration", invalid)
            latest = replies[-1]
            self.assertEqual(latest["errors"][0]["path"], "/sketch_library/0/element_ids/0")
            return self.call(
                "repair_analysis_submission",
                {
                    "draft_id": latest["draft_id"],
                    "expected_revision": latest["revision"],
                    "changes": [{"op": "set", "path": "/sketch_library/0/element_ids/0", "value": "E1"}],
                },
            )

        self.response = respond
        outcome = self.run_worker()
        self.assertEqual(outcome.execution.status, "completed", outcome.error)
        self.assertEqual(outcome.report.sketch_library[0].composition, ("均匀色面",))
        self.assertEqual(outcome.execution.model_calls, 2)
        self.assertTrue((self.root / "submissions/worker-1.json").exists())
        payload, blocks = request_context(self.requests[-1])
        self.assertNotIn("submission", payload)
        self.assertTrue(any("运行控制反馈" in block.get("text", "") for block in blocks))

    def test_outline_issue_does_not_end_exploration_or_modify_outline(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if not tool_results(request):
                return self.call("report_outline_issue", {"region": "右下角", "description": "可能遗漏小点", "element_ids": []})
            return self.call("submit_exploration", REPORT)

        self.response = respond
        outcome = self.run_worker()
        self.assertEqual(outcome.execution.status, "completed", outcome.error)
        self.assertEqual(len(outcome.issues), 1)
        self.assertEqual(outcome.issues[0].description, "可能遗漏小点")
        self.assertEqual(len(self.outline.elements), 1)

    def test_empty_report_is_rejected_then_stop_preserves_reason(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if not tool_results(request):
                return self.call("submit_exploration", {"sketch_library": [], "feature_library": [], "relation_library": []})
            return self.call("stop_exploration", {"reason": "无法解释初稿遗漏的对象"})

        self.response = respond
        outcome = self.run_worker()
        self.assertIsNone(outcome.report)
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertEqual(outcome.error, "无法解释初稿遗漏的对象")
        self.assertEqual(outcome.execution.model_calls, 2)
        events = [json.loads(line)["event"] for line in (self.root / "events.jsonl").read_text().splitlines()]
        self.assertNotIn("task_completed", events)
        self.assertEqual(events[-1], "task_stopped")

    def test_budget_exhaustion_keeps_failure_and_no_fabricated_report(self) -> None:
        self.worker_options = AnalysisOptions(max_worker_calls=1, max_request_retries=0)
        self.response = lambda _: {"role": "assistant", "content": "尚不确定"}
        outcome = self.run_worker()
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertEqual(outcome.execution.model_calls, 1)
        self.assertIsNone(outcome.report)
        self.assertIn("budget exhausted", outcome.error)

    def test_commit_failure_keeps_draft_and_retries_without_false_success(self) -> None:
        commits: list[ExplorationReport] = []

        def commit(report: ExplorationReport) -> dict[str, object]:
            commits.append(report)
            if len(commits) == 1:
                msg = "snapshot unavailable"
                raise OSError(msg)
            return {"status": "committed", "report_id": "worker-1", "sketches": 1}

        def respond(request: dict[str, object]) -> dict[str, object]:
            replies = tool_results(request)
            if replies:
                self.assertEqual(replies[-1]["status"], "invalid_submission")
                self.assertIn("snapshot unavailable", replies[-1]["errors"][0]["message"])
                draft = json.loads((self.root / "submissions/worker-1.json").read_text())
                self.assertNotEqual(draft.get("status"), "submitted")
                self.assertEqual(draft["arguments"], REPORT)
            return self.call("submit_exploration", REPORT)

        self.response = respond
        outcome = self.run_worker(commit_report=commit)
        self.assertEqual(outcome.execution.status, "completed", outcome.error)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0], commits[1])
        draft = json.loads((self.root / "submissions/worker-1.json").read_text())
        self.assertEqual(draft["status"], "submitted")

    def test_commit_replay_is_idempotent_and_changed_payload_is_rejected(self) -> None:
        commits: list[ExplorationReport] = []
        changed = deepcopy(REPORT)
        changed["sketch_library"][0]["name"] = "不同组成"

        def commit(report: ExplorationReport) -> dict[str, object]:
            commits.append(report)
            return {"status": "committed", "report_id": "worker-1"}

        def run(loop: AnalysisLoop, *_args: object) -> None:
            handler = loop.submission_handler
            first = handler.handle("submit_exploration", REPORT)
            replay = handler.handle("submit_exploration", deepcopy(REPORT))
            self.assertEqual(first.content, replay.content)
            rejected = json.loads(handler.handle("submit_exploration", changed).content)
            self.assertEqual(rejected["status"], "invalid_submission")
            self.assertIn("already committed a different", rejected["errors"][0]["message"])

        with patch("shader_deep.agents.exploration.agent.AnalysisLoop.run", autospec=True, side_effect=run):
            outcome = self.run_worker(commit_report=commit)
        self.assertEqual(outcome.execution.status, "completed")
        self.assertEqual(len(commits), 1)
        self.assertEqual(outcome.report.sketch_library[0].name, "平面组成")

    def test_failed_commit_keeps_outline_issues_and_never_returns_report(self) -> None:
        self.worker_options = AnalysisOptions(max_worker_calls=2, max_request_retries=0)

        def commit(_report: ExplorationReport) -> dict[str, object]:
            msg = "storage failed"
            raise OSError(msg)

        def respond(request: dict[str, object]) -> dict[str, object]:
            if not tool_results(request):
                return self.call("report_outline_issue", {"region": "右侧", "description": "遗漏对象", "element_ids": []})
            return self.call("submit_exploration", REPORT)

        self.response = respond
        outcome = self.run_worker(commit_report=commit)
        self.assertIsNone(outcome.report)
        self.assertNotEqual(outcome.execution.status, "completed")
        self.assertEqual(outcome.issues[0].description, "遗漏对象")
        draft = json.loads((self.root / "submissions/worker-1.json").read_text())
        self.assertEqual(draft["arguments"], REPORT)
        self.assertIn("storage failed", draft["errors"][0]["message"])

    def test_cancel_before_exploration_sends_no_request(self) -> None:
        commits: list[ExplorationReport] = []

        def commit(report: ExplorationReport) -> dict[str, object]:
            commits.append(report)
            return {"status": "stored"}

        outcome = self.run_worker(commit_report=commit, should_stop=lambda: True)
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertIn("cancelled", outcome.error)
        self.assertEqual(self.requests, [])
        self.assertEqual(commits, [])
        self.assertIsNone(outcome.report)

    def test_cancel_before_commit_does_not_publish_received_response(self) -> None:
        commits: list[ExplorationReport] = []

        def commit(report: ExplorationReport) -> dict[str, object]:
            commits.append(report)
            return {"status": "stored"}

        self.response = lambda _: self.call("submit_exploration", REPORT)
        outcome = self.run_worker(commit_report=commit, should_stop=lambda: bool(self.requests))
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(commits, [])
        self.assertIsNone(outcome.report)
        draft = json.loads((self.root / "submissions/worker-1.json").read_text())
        self.assertEqual(draft["arguments"], REPORT)
        self.assertNotEqual(draft.get("status"), "submitted")

    def test_cancel_during_repair_preserves_invalid_draft_without_next_request(self) -> None:
        invalid = deepcopy(REPORT)
        invalid["sketch_library"][0]["element_ids"] = ["missing"]
        self.response = lambda _: self.call("submit_exploration", invalid)
        outcome = self.run_worker(should_stop=lambda: bool(self.requests))
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(outcome.report)
        draft = json.loads((self.root / "submissions/worker-1.json").read_text())
        self.assertEqual(draft["arguments"], invalid)
        self.assertTrue(draft["errors"])

    def test_cancellation_after_commit_retains_the_committed_report(self) -> None:
        cancelled: list[bool] = []

        def commit(_report: ExplorationReport) -> dict[str, object]:
            cancelled.append(True)
            return {"status": "stored"}

        self.response = lambda _: self.call("submit_exploration", REPORT)
        outcome = self.run_worker(commit_report=commit, should_stop=lambda: bool(cancelled))
        self.assertEqual(outcome.execution.status, "completed")
        self.assertIsNotNone(outcome.report)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(cancelled), 1)

    def test_cancel_preserves_previously_reported_outline_issue(self) -> None:
        self.response = lambda _: self.call("report_outline_issue", {"region": "右侧", "description": "遗漏对象", "element_ids": []})
        outcome = self.run_worker(should_stop=lambda: bool(self.requests))
        self.assertEqual(outcome.execution.status, "stopped")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(outcome.issues[0].description, "遗漏对象")
