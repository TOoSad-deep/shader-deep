"""按需会话的累计选材预算、呈现边界和必要工作终态."""

from __future__ import annotations

import json
from dataclasses import replace

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage, ToolMessage

from shader_deep.analysis.exploration import Candidate
from shader_deep.analysis.exploration_prompts import INTEGRATION_PROMPT
from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.exploration_worker import ExplorationOutcome
from shader_deep.analysis.request_budget import check_request, remaining_material_chars
from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions
from shader_deep.blackboard import add_task
from shader_deep.config import build_model
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TaskRecord
from tests.unit_tests._generation_fixture import GenerationFixture
from tests.unit_tests.test_possibility_library import outline, report


class OnDemandSessionTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="比较外观"))
        self.session = ExplorationSession(state, "A1", AnalysisOptions(max_request_retries=0), self.root, png_data_url(self.root / "reference.PNG"))
        self.session._submit_outline(outline(), ["平面组织", "空间形态"])
        for index, task in enumerate(self.session._register()):
            source = report("隐藏候选正文")
            feature = replace(source.feature_library[0], appearance=("A" if index == 0 else "B") * 8000)
            source = replace(source, feature_library=(feature,))
            self.session._commit_report(task, source)
            outcome = ExplorationOutcome(report=source, execution=AnalysisExecution(status="completed"), issues=(), error=None)
            self.session._collect(task, outcome)
        self.features = [item.id for item in self.session.store.library.feature_library]
        self.session._tools = self.session.integration_tools()

    def read(self, identities: list[str], *, first: bool = False) -> dict[str, object]:
        arguments = {"requests": [{"library": "feature_library", "ids": identities}]}
        if first:
            arguments["comparison"] = {"question": "比较外观", "target_ids": [identities[0]]}
        return json.loads(self.session._read_library(arguments))

    def test_initial_context_is_index_only_and_finish_inherits_unread_content(self) -> None:
        context = json.dumps(self.session.context().content, ensure_ascii=False)
        self.assertNotIn("A" * 100, context)
        self.assertNotIn("隐藏候选正文", context)
        result = json.loads(self.session._finish_analysis())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.session.store.presented)
        self.assertEqual(len(self.session.summary_result.analysis_detail.feature_library), 2)
        self.assertFalse(self.session.comparisons.pending)

    def test_cumulative_admission_keeps_old_body_and_returns_omitted_ids(self) -> None:
        self.session.comparisons.start(self.session.store, "比较外观", [self.features[0]])
        options = self.session.options
        available = remaining_material_chars(INTEGRATION_PROMPT, self.session.context(), self.session._tools, options)
        self.session.options = replace(options, max_context_tokens=options.max_context_tokens - available + 12000)
        first = self.read([self.features[0]])
        second = self.read([self.features[1]])
        self.assertEqual(first["selected_ids"], [self.features[0]])
        self.assertEqual(second["not_provided_ids"], [self.features[1]])
        self.assertEqual(list(self.session.comparisons.active.entries), [self.features[0]])
        self.assertFalse(self.session.comparisons.active.presented_versions)
        duplicate = self.read([self.features[0]])
        self.assertEqual(duplicate["duplicate_ids"], [self.features[0]])
        message = self.session.context()
        request = ModelRequest(model=build_model(), messages=[message], system_message=SystemMessage(content=INTEGRATION_PROMPT))
        check_request(request, self.session._tools, self.session.options)
        self.session.on_prepared([message])
        self.session._submit_integration({"preserve": True})
        self.assertTrue(self.session.group_accepted)

    def test_requested_target_not_presented_cannot_be_preserved_or_finished(self) -> None:
        self.read([self.features[0]], first=True)
        with self.assertRaisesRegex(ValueError, "present"):
            self.session._submit_integration({"preserve": True})
        with self.assertRaisesRegex(ValueError, "active comparison"):
            self.session._finish_analysis()
        self.session._submit_integration({"deferred_work": ["必要正文尚未呈现"]})
        self.session.group_accepted = False
        self.session._finish_analysis()
        self.assertEqual(self.session.summary_result.status, "partial")
        latest = json.loads((self.session.store.directory / f"version-{self.session.store.revision:04d}.json").read_text())
        progress = next(iter(latest["integration_progress"].values()))
        self.assertEqual(progress["status"], "deferred")

    def test_reading_another_scope_does_not_clear_required_review(self) -> None:
        self.read([self.features[0]], first=True)
        self.session.on_prepared([self.session.context()])
        self.session._submit_integration({"preserve": True, "review_ids": [self.features[1]]})
        pending = self.session.comparisons.pending[0]["work_id"]
        self.session.group_accepted = False
        self.read([self.features[1]], first=True)
        self.session.on_prepared([self.session.context()])
        self.session._submit_integration({"preserve": True})
        self.assertEqual(self.session.comparisons.pending[0]["work_id"], pending)
        self.session.group_accepted = False
        self.session._finish_analysis()
        self.assertEqual(self.session.summary_result.status, "partial")

    def test_old_group_presentation_cannot_authorize_new_unseen_auxiliary_body(self) -> None:
        self.read(self.features, first=True)
        self.session.on_prepared([self.session.context()])
        self.session._submit_integration({"preserve": True})
        self.session.group_accepted = False
        self.session._read_library(
            {
                "comparison": {"question": "尝试合并", "target_ids": [self.features[0]]},
                "requests": [{"library": "feature_library", "ids": [self.features[0]]}],
            }
        )
        self.session.on_prepared([self.session.context()])
        self.session._read_library({"extend_target_ids": [self.features[1]], "requests": [{"library": "feature_library", "ids": [self.features[1]]}]})
        with self.assertRaisesRegex(ValueError, "present"):
            self.session._submit_integration({"merges": [{"kind": "feature", "members": self.features}]})
        self.assertEqual(len(self.session.store.library.feature_library), 2)

    def add_large_candidate_owner(self) -> tuple[str, list[str]]:
        source = report()
        candidates = (
            source.feature_library[0].candidates[0],
            *(Candidate(id=f"C{index}_" + "x" * 250, mechanism="m" * 4000, reasoning="需要比较") for index in range(80)),
        )
        source = replace(source, feature_library=(replace(source.feature_library[0], candidates=candidates),))
        self.session.store.add_report("bulk", source)
        return "bulk::F", [f"bulk::{candidate.id}" for candidate in candidates]

    def assert_receipt_fits_request(self, receipt: str) -> None:
        request = ModelRequest(
            model=build_model(),
            messages=[ToolMessage(content=receipt, tool_call_id="read"), self.session.context()],
            system_message=SystemMessage(content=INTEGRATION_PROMPT),
        )
        check_request(request, self.session._tools, self.session.options)

    def test_large_read_reserves_receipt_in_tool_history_and_control_context(self) -> None:
        owner, candidates = self.add_large_candidate_owner()
        self.session.options = replace(self.session.options, max_context_tokens=128000)
        receipt = self.session._read_library(
            {
                "comparison": {"question": "比较大量候选", "target_ids": [owner]},
                "requests": [{"library": "feature_library", "ids": [owner], "include_candidates": True}],
            }
        )
        result = json.loads(receipt)
        self.assertTrue(result["selected_ids"])
        self.assertTrue(result["not_provided_ids"])
        self.assertEqual(set(result["selected_ids"]) | set(result["not_provided_ids"]), {owner, *candidates})
        self.assert_receipt_fits_request(receipt)
        self.assertFalse(self.session.comparisons.active.presented_versions)

    def test_oversized_receipt_leaves_selection_intact_and_smaller_retry_works(self) -> None:
        owner, candidates = self.add_large_candidate_owner()
        self.session.comparisons.start(self.session.store, "比较大量候选", [owner])
        self.session._read_library({"requests": [{"library": "feature_library", "ids": [owner]}]})
        options = self.session.options
        available = remaining_material_chars(INTEGRATION_PROMPT, self.session.context(), self.session._tools, options)
        self.session.options = replace(options, max_context_tokens=options.max_context_tokens - available + 35000)
        before = dict(self.session.comparisons.active.entries)
        with self.assertRaisesRegex(ValueError, "receipt") as failure:
            self.session._read_library({"requests": [{"library": "feature_library", "ids": [owner], "include_candidates": True}]})
        self.assertEqual(self.session.comparisons.active.entries, before)
        self.assert_receipt_fits_request(str(failure.exception))
        receipt = self.session._read_library(
            {
                "requests": [{"library": "feature_library", "ids": [owner], "include_candidates": True, "candidate_ids": [candidates[0]]}],
            }
        )
        self.assertEqual(json.loads(receipt)["selected_ids"], [candidates[0]])
        self.assertEqual(json.loads(receipt)["duplicate_ids"], [owner])
        self.assert_receipt_fits_request(receipt)
        self.session.on_prepared([self.session.context()])
        self.session._submit_integration({"preserve": True})
        self.assertTrue(self.session.group_accepted)

    def test_stop_after_finish_cannot_rewrite_terminal_state(self) -> None:
        receipt = self.session._finish_analysis()
        result = self.session.summary_result
        snapshot = (self.root / "run.json").read_bytes()
        replay = self.session._stop_tool().invoke({"reason": "同一响应的稍后停止调用"})
        self.assertEqual(replay, receipt)
        self.assertEqual(self.session.stop_reason, "completed")
        self.assertIs(self.session.summary_result, result)
        self.assertEqual((self.root / "run.json").read_bytes(), snapshot)
        self.assertIsNone(self.session.execution.error)

    def test_repeated_stop_preserves_original_reason(self) -> None:
        stop = self.session._stop_tool()
        first = stop.invoke({"reason": "原始停止原因"})
        self.assertEqual(stop.invoke({"reason": "第二个原因"}), first)
        self.assertEqual(self.session.execution.error, "原始停止原因")

    def test_unknown_review_id_returns_repairable_index_and_preserves_valid_review(self) -> None:
        self.read([self.features[0]], first=True)
        self.session.on_prepared([self.session.context()])
        handler = self.session.submission_handler
        revision = self.session.store.revision
        response = handler.handle("submit_integration", {"preserve": True, "review_ids": [self.features[1], "missing"]})
        rejection = json.loads(response.content)
        self.assertEqual(rejection["errors"][0]["path"], "/review_ids/1")
        self.assertEqual(self.session.store.revision, revision)
        repaired = handler.handle(
            "repair_analysis_submission",
            {
                "draft_id": rejection["draft_id"],
                "expected_revision": rejection["revision"],
                "changes": [{"op": "remove", "path": rejection["errors"][0]["path"]}],
            },
        )
        self.assertIsNone(repaired.category)
        self.assertEqual(self.session.comparisons.pending[0]["target_ids"], [self.features[1]])
