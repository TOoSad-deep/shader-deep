"""固定工作清单保护小范围整合、依赖推进及有界失败恢复."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from unittest.mock import patch

from langchain_core.exceptions import ModelError

from shader_deep.agents.exploration.contracts import ExplorationOutcome
from shader_deep.compatibility.controlled import ControlledSession, ControlledWork
from shader_deep.domain.blackboard import add_task
from shader_deep.domain.tasks import TaskRecord
from shader_deep.experiments.controlled_replay import _write_summary
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.domain.test_possibility_library import outline, report
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture

SECOND_MODEL_CALL = 2


class ControlledDedupTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="比较已有结论"))
        options = AnalysisOptions(max_main_calls=12)
        self.session = ControlledSession(state, "A1", options, self.root, png_data_url(self.root / "reference.PNG"))
        self.session._submit_outline(outline(), ["外观", "组成"])
        for index, task in enumerate(self.session._register()):
            source = report("单层模糊")
            feature = source.feature_library[0]
            distinct = replace(feature.candidates[1], mechanism=("多层投影", "渐变环")[index])
            feature = replace(feature, candidates=(feature.candidates[0], distinct))
            independent = replace(feature, id="G", name="底场晕影", appearance="底场渐变", candidates=(replace(feature.candidates[0], id="V"),))
            source = replace(source, feature_library=(feature, independent))
            self.session._commit_report(task, source)
            self.session._collect(task, ExplorationOutcome(report=source, execution=AnalysisExecution(status="completed"), issues=(), error=None))
        self.initial_library = self.session.store.library
        features = self.initial_library.feature_library
        self.owners = tuple(feature.id for feature in features if feature.name == "亮边")
        self.independent = tuple(feature.id for feature in features if feature.name == "底场晕影")
        self.candidates = tuple(candidate.id for feature in features if feature.id in self.owners for candidate in feature.candidates)
        self.works = (
            ControlledWork(id="W1", kind="feature", target_ids=tuple(reversed(self.owners)), question="是否相同外观"),
            ControlledWork(id="W2", kind="candidate", target_ids=self.candidates, question="比较机制", depends_on="W1", owner_ids=self.owners),
            ControlledWork(id="W3", kind="feature", target_ids=self.independent, question="独立比较"),
        )

    def run_works(self, *, max_work_calls: int = 4) -> None:
        self.session.run_controlled(build_model(), self.works, max_work_calls=max_work_calls)

    def merge(self, kind: str, members: tuple[str, ...], *, preserve: bool = False) -> dict[str, object]:
        return {"merges": [{"kind": kind, "members": list(members)}], "preserve": preserve}

    def repair(self, changes: list[dict[str, object]]) -> dict[str, object]:
        draft = self.session.submission_handler.draft
        return self.call(
            "repair_analysis_submission",
            {"draft_id": draft["draft_id"], "expected_revision": draft["revision"], "changes": changes},
        )

    def test_three_accepted_decisions_use_canonical_owner_and_keep_distinct_mechanisms(self) -> None:
        versions = []

        def respond(request: dict[str, object]) -> dict[str, object]:
            versions.append(self.session.store.revision)
            # 验证真实传输请求按类型切换规则, 不把模拟模型结果当作语义效果证明.
            system = "\n".join(message["content"] for message in request["messages"] if message["role"] == "system")
            expected, excluded = (
                ("candidate-criteria", "feature-criteria") if len(self.requests) == SECOND_MODEL_CALL else ("feature-criteria", "candidate-criteria")
            )
            self.assertIn(expected, system)
            self.assertNotIn(excluded, system)
            names = {tool["function"]["name"] for tool in request["tools"]}
            self.assertEqual(names, {"submit_integration", "repair_analysis_submission"})
            self.assertNotIn("image_url", json.dumps(request["messages"]))
            if len(self.requests) == 1:
                return self.call("submit_integration", self.merge("feature", self.owners))
            if len(self.requests) == SECOND_MODEL_CALL:
                canonical = self.session.store.resolve(self.owners[0])
                self.assertEqual(canonical, self.session.store.resolve(self.owners[1]))
                owner = next(feature for feature in self.session.store.library.feature_library if feature.id == canonical)
                self.assertEqual({candidate.id for candidate in owner.candidates}, set(self.candidates))
                material = json.dumps(request["messages"], ensure_ascii=False)
                for identifier in (canonical, *self.candidates):
                    self.assertIn(identifier, material)
                for mechanism in ("单层模糊", "多层投影", "渐变环"):
                    self.assertIn(mechanism, material)
                self.assertTrue(set(self.candidates).issubset(self.session.comparisons.active.presented_versions))
                return self.call("submit_integration", self.merge("candidate", (self.candidates[0], self.candidates[2]), preserve=True))
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        with patch.multiple("shader_deep.compatibility.controlled", FEATURE_PROMPT="feature-criteria", CANDIDATE_PROMPT="candidate-criteria"):
            self.run_works()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(versions, [2, 3, 4])
        self.assertTrue(all(result["receipt"] for result in self.session.work_results))
        self.assertEqual([result["model_calls"] for result in self.session.work_results], [1, 1, 1])
        owner = next(feature for feature in self.session.store.library.feature_library if feature.id == self.session.store.resolve(self.owners[0]))
        self.assertEqual({candidate.mechanism for candidate in owner.candidates}, {"单层模糊", "多层投影", "渐变环"})
        self.assertEqual(len(self.session.store.provenance[owner.id]), 2)
        self.assertEqual(len(self.session.store.provenance[self.session.store.resolve(self.candidates[0])]), 2)

    def test_preserve_skips_dependent_work_but_accepts_next_independent_work(self) -> None:
        self.response = lambda _: self.call("submit_integration", {"preserve": True})
        self.run_works()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.session.work_results[1]["status"], "not_run")
        self.assertTrue(self.session.work_results[1]["reason"])
        self.assertTrue(self.session.work_results[2]["receipt"])
        self.assertNotEqual(self.session.store.resolve(self.owners[0]), self.session.store.resolve(self.owners[1]))

    def test_deferred_work_does_not_reenter_or_reset_spent_calls(self) -> None:
        def respond(_: dict[str, object]) -> dict[str, object]:
            arguments = {"deferred_work": ["缺少轮廓方向证据"]} if len(self.requests) == 1 else {"preserve": True}
            return self.call("submit_integration", arguments)

        self.response = respond
        self.run_works()
        before = self.session.execution.model_calls
        results = list(self.session.work_results)
        with self.assertRaisesRegex(ValueError, "scheduled"):
            self.run_works()
        self.assertEqual(self.session.execution.model_calls, before)
        self.assertEqual(self.session.work_results, results)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.session.work_results[1]["status"], "not_run")
        self.assertTrue(self.session.work_results[2]["receipt"])
        self.assertTrue(self.session.comparisons.deferred)
        self.assertEqual(self.session.store.library, self.initial_library)

    def test_work_limit_defers_current_work_and_continues_independent_work(self) -> None:
        def respond(_: dict[str, object]) -> dict[str, object]:
            if len(self.requests) <= SECOND_MODEL_CALL:
                return {"role": "assistant", "content": "继续比较, 尚未提交"}
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        self.run_works(max_work_calls=2)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.session.work_results[0]["status"], "deferred")
        self.assertEqual(self.session.work_results[0]["model_calls"], 2)
        self.assertEqual(self.session.work_results[1]["status"], "not_run")
        self.assertTrue(self.session.work_results[2]["receipt"])

    def test_global_limit_keeps_published_merge_and_does_not_start_later_decisions(self) -> None:
        self.session.options = replace(self.session.options, max_main_calls=1)
        self.response = lambda _: self.call("submit_integration", self.merge("feature", self.owners))
        self.run_works()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session.store.revision, 3)
        self.assertEqual(self.session.store.resolve(self.owners[0]), self.session.store.resolve(self.owners[1]))
        self.assertEqual(self.session.execution.model_calls, 1)
        self.assertFalse(any(result["receipt"] for result in self.session.work_results[1:]))

    def test_oversized_work_is_deferred_without_presentation_and_next_group_still_runs(self) -> None:
        store = self.session.store
        features = tuple(
            replace(feature, appearance="范围细节" * 30000) if feature.id in self.owners else feature for feature in store.library.feature_library
        )
        store._commit(asdict(replace(store.library, feature_library=features)), store.provenance, store.aliases)
        self.session.options = replace(self.session.options, max_context_tokens=30000)
        self.response = lambda _: self.call("submit_integration", {"preserve": True})
        self.run_works()
        self.assertEqual(len(self.requests), 1)
        result = self.session.work_results[0]
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["presented_versions"], {})
        self.assertTrue(result["reason"])
        self.assertTrue(self.session.work_results[2]["receipt"])

    def test_provider_failure_records_attempted_work_and_summary_leaves_later_work_not_run(self) -> None:
        def unavailable(_: dict[str, object]) -> dict[str, object]:
            msg = "模拟连接中断"
            raise RuntimeError(msg)

        self.session.options = replace(self.session.options, max_request_retries=0)
        self.response = unavailable
        with self.assertRaises(ModelError):
            self.run_works()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.session.work_results), 1)
        result = self.session.work_results[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_calls"], 1)
        self.assertTrue(result["comparison_id"])
        self.assertTrue(set(self.owners).issubset(result["presented_versions"]))
        self.assertIsNone(result["receipt"])
        self.assertIn("Connection", result["reason"])
        self.assertIsNone(self.session.comparisons.active)
        self.assertTrue(self.session.comparisons.deferred)
        _write_summary(self.session, self.works)
        summary = json.loads((self.root / "controlled-summary.json").read_text())
        self.assertEqual([step["status"] for step in summary["steps"]], ["failed", "not_run", "not_run"])
        self.assertEqual([step["model_calls"] for step in summary["steps"]], [1, 0, 0])
        self.assertFalse(summary["three_step_flow_passed"])

    def test_wrong_kind_and_scope_are_rejected_before_valid_local_decision(self) -> None:
        def respond(_: dict[str, object]) -> dict[str, object]:
            self.assertEqual(self.session.store.library, self.initial_library)
            if len(self.requests) == 1:
                return self.call("submit_integration", self.merge("candidate", self.candidates[:2]))
            if len(self.requests) == SECOND_MODEL_CALL:
                return self.call("submit_integration", self.merge("feature", (self.owners[0], self.independent[0])))
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        self.works = self.works[:1]
        self.run_works()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.session.work_results[0]["receipt"])
        self.assertEqual(self.session.store.library, self.initial_library)

    def test_repair_cannot_inject_disallowed_fields_and_can_remove_them(self) -> None:
        def respond(_: dict[str, object]) -> dict[str, object]:
            self.assertEqual(self.session.store.library, self.initial_library)
            if len(self.requests) == 1:
                return self.call("submit_integration", {"preserve": True, "additions": {}})
            if len(self.requests) == SECOND_MODEL_CALL:
                return self.repair([{"op": "set", "path": "", "value": {"preserve": True, "unresolved": {"E": ["越权修改"]}}}])
            self.assertNotEqual(self.session.submission_handler.draft.get("status"), "submitted")
            return self.repair([{"op": "remove", "path": "/unresolved"}])

        self.response = respond
        self.works = self.works[:1]
        self.run_works()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.session.work_results[0]["receipt"])
        self.assertEqual(self.session.store.library.elements[0].unresolved, ())

    def test_defer_cannot_publish_partial_merge(self) -> None:
        def respond(_: dict[str, object]) -> dict[str, object]:
            self.assertEqual(self.session.store.library, self.initial_library)
            if len(self.requests) == 1:
                return self.call("submit_integration", {**self.merge("feature", self.owners), "deferred_work": ["剩余判断缺少证据"]})
            return self.call("submit_integration", {"deferred_work": ["整个工作仍缺证据"]})

        self.response = respond
        self.works = self.works[:1]
        self.run_works()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.session.store.library, self.initial_library)
        self.assertEqual(self.session.work_results[0]["status"], "deferred")
