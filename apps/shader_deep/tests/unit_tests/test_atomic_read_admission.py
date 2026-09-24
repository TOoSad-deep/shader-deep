"""范围变更在正文及预算全部准入之后发布, 辅助读取仍可部分接受."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from shader_deep.analysis.exploration_prompts import INTEGRATION_PROMPT
from shader_deep.analysis.exploration_session import ExplorationSession, RepairContextError
from shader_deep.analysis.exploration_worker import ExplorationOutcome
from shader_deep.analysis.request_budget import remaining_material_chars
from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions, ToolFeedback
from shader_deep.blackboard import add_task
from shader_deep.config import build_model
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TaskRecord
from tests.unit_tests._generation_fixture import GenerationFixture
from tests.unit_tests.test_possibility_library import outline, report


class AtomicReadAdmissionTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="比较"))
        self.session = ExplorationSession(state, "A1", AnalysisOptions(), self.root, png_data_url(self.root / "reference.PNG"))
        self.session._submit_outline(outline(), ["平面", "空间"])
        for task in self.session._register():
            source = report()
            self.session._commit_report(task, source)
            self.session._collect(task, ExplorationOutcome(report=source, execution=AnalysisExecution(status="completed"), issues=(), error=None))
        self.features = [item.id for item in self.session.store.library.feature_library]
        self.session._tools = self.session.integration_tools()

    def arguments(self, targets: list[str], reads: list[str]) -> dict[str, object]:
        return {
            "comparison": {"question": "比较外观", "target_ids": targets},
            "requests": [{"library": "feature_library", "ids": reads}],
        }

    def test_missing_initial_target_and_budget_rejection_leave_no_work(self) -> None:
        before = self.session.comparisons.snapshot()
        with self.assertRaisesRegex(ValueError, "target_materials_missing"):
            self.session._read_library(self.arguments(self.features, self.features[:1]))
        self.assertEqual(self.session.comparisons.snapshot(), before)
        self.session.options = replace(self.session.options, max_context_tokens=100)
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.session._read_library(self.arguments(self.features, self.features))
        self.assertEqual(self.session.comparisons.snapshot(), before)

    def test_extension_rejection_preserves_scope_versions_and_bodies(self) -> None:
        self.session._read_library(self.arguments(self.features[:1], self.features[:1]))
        self.session.on_prepared([self.session.context()])
        before = self.session.comparisons.snapshot()
        with self.assertRaisesRegex(ValueError, "target_materials_missing"):
            self.session._read_library(
                {"extend_target_ids": self.features[1:], "requests": [{"library": "feature_library", "ids": self.features[:1]}]}
            )
        self.assertEqual(self.session.comparisons.snapshot(), before)
        self.session.options = replace(self.session.options, max_comparison_targets=1)
        with self.assertRaisesRegex(ValueError, "target_scope_exceeded"):
            self.session._read_library(
                {"extend_target_ids": self.features[1:], "requests": [{"library": "feature_library", "ids": self.features[1:]}]}
            )
        self.assertEqual(self.session.comparisons.snapshot(), before)

    def test_oversized_required_work_is_not_lost_on_resume_failure(self) -> None:
        self.session.comparisons.start(self.session.store, "必要比较", self.features)
        self.session.comparisons.defer("等待材料")
        work_id = self.session.comparisons.pending[0]["work_id"]
        before = self.session.comparisons.snapshot()
        self.session.options = replace(self.session.options, max_comparison_targets=1)
        with self.assertRaisesRegex(ValueError, "target_scope_exceeded"):
            self.session._read_library({"work_id": work_id, "requests": [{"library": "feature_library", "ids": self.features}]})
        self.assertEqual(self.session.comparisons.snapshot(), before)
        self.session._finish_analysis()
        self.assertEqual(self.session.summary_result.status, "partial")

    def test_current_draft_replaces_old_parameters_and_remains_repairable(self) -> None:
        self.session._read_library(self.arguments(self.features[:1], self.features[:1]))
        self.session.on_prepared([self.session.context()])
        arguments = {"preserve": True, "review_ids": ["missing"], "unused": "保留完整草稿" * 8000}
        rejected = self.session.submission_handler.handle("submit_integration", arguments)
        messages = [
            AIMessage(content="", tool_calls=[{"name": "submit_integration", "args": arguments, "id": "draft"}]),
            ToolMessage(content=rejected.content, tool_call_id="draft"),
        ]
        compacted = self.session._prepare_history(messages)
        self.assertEqual(compacted[0].tool_calls[0]["args"], {})
        context = self.session.context()
        self.assertIn(arguments["unused"], json.loads(context.content[-1]["text"].split(": ", 1)[1])["submission"]["arguments"]["unused"])
        self.session.options = replace(self.session.options, max_context_tokens=20000)
        request = ModelRequest(model=build_model(), messages=[*compacted, context], system_message=SystemMessage(content=INTEGRATION_PROMPT))
        with self.assertRaisesRegex(RepairContextError, "repair_context_exceeded"):
            self.session._check_request(request)
        self.assertEqual(self.session.execution.model_calls, 0)
        saved = json.loads(next((self.root / "submissions").glob("*integration*.json")).read_text())
        self.assertEqual(saved["arguments"], arguments)
        self.assertEqual(self.session.store.revision, 2)

    def test_resume_cannot_omit_unread_defer_reason(self) -> None:
        self.session.comparisons.start(self.session.store, "必要比较", self.features[:1])
        self.session.comparisons.defer("必须核对完整判断依据" * 30000)
        work_id = self.session.comparisons.pending[0]["work_id"]
        before = self.session.comparisons.snapshot()
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.session._read_library({"work_id": work_id, "requests": [{"library": "feature_library", "ids": self.features[:1]}]})
        self.assertEqual(self.session.comparisons.snapshot(), before)
        self.assertIsNone(self.session.comparisons.active)

    def test_unpresented_outline_issue_cannot_be_dismissed(self) -> None:
        self.session.issues = [{"id": "issue", "region": "全图", "description": "尚未呈现"}]
        self.session._read_library(self.arguments(self.features[:1], self.features[:1]))
        with self.assertRaisesRegex(ValueError, "presented issue"):
            self.session._submit_integration(
                {"preserve": True, "outline_issue_decisions": [{"issue_id": "issue", "disposition": "dismissed", "reason": "没有看到原问题"}]}
            )

    def test_query_admission_keeps_actual_prior_control_receipt(self) -> None:
        self.session._read_library(self.arguments(self.features[:1], self.features[:1]))
        self.session.last_action = {"selected_ids": ["x" * 20000]}
        page = {"entries": [], "total": 0, "remaining": 0, "next_cursor": None}
        options = self.session.options
        predicted = remaining_material_chars(INTEGRATION_PROMPT, self.session._build_context(last_action=page), self.session._tools, options)
        self.session.options = replace(options, max_context_tokens=options.max_context_tokens - predicted + len(json.dumps(page)) + 200)
        before = dict(self.session.last_action)
        with self.assertRaisesRegex(ValueError, "query_history_exceeded"):
            self.session._query_receipt(page)
        self.assertEqual(self.session.last_action, before)

    def test_system_defer_continues_discovery_without_charging_old_repair(self) -> None:
        self.session._read_library(self.arguments(self.features[:1], self.features[:1]))
        work_id = self.session.comparisons.active.id
        self.session.execution.model_calls = 1
        self.session.execution.format_repair_scopes[work_id] = 2
        self.session.execution.format_repair_calls = 2
        self.session.execution.tool_feedback = (
            ToolFeedback(model_call=1, tool="submit_integration", message="bad", category="schema_validation", repair_scope=work_id),
        )
        original = self.session._run_phase
        phases = []

        def run_phase(model: object, *, initial: bool) -> None:
            phases.append(initial)
            if len(phases) == 1:
                msg = "repair_context_exceeded"
                raise RepairContextError(msg)
            original(model, initial=initial)

        self.response = lambda _request: self.call("finish_analysis", {})
        with patch.object(self.session, "_run_phase", side_effect=run_phase):
            self.session._integrate(build_model())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session.execution.format_repair_scopes[work_id], 2)
        self.assertEqual(self.session.summary_result.status, "partial")
        self.assertEqual(self.session.comparisons.deferred[0]["work_id"], work_id)

    def test_successful_group_creation_retires_same_response_discovery_error(self) -> None:
        self.session.execution.format_repair_scopes["discovery"] = 2
        self.session.execution.format_repair_calls = 2

        def respond(_request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                invalid = self.call("read_library", {"requests": "invalid"})
                valid = self.call("read_library", self.arguments(self.features[:1], self.features[:1]))
                valid["tool_calls"][0]["id"] = "valid-after-schema-error"
                invalid["tool_calls"].extend(valid["tool_calls"])
                return invalid
            if self.session.comparisons.active is not None:
                return self.call("submit_integration", {"preserve": True})
            return self.call("finish_analysis", {})

        self.response = respond
        self.session._integrate(build_model())
        self.assertEqual(self.session.stop_reason, "completed")
        self.assertEqual(self.session.execution.format_repair_scopes, {"discovery": 2})
        self.assertEqual(self.session.execution.format_repair_calls, 2)
        self.assertEqual(len(self.requests), 3)
