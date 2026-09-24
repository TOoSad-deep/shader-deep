"""验证过期决定由后端刷新材料后重试, 不静默套用到新正文."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

from shader_deep.agents.exploration.contracts import ExplorationOutcome
from shader_deep.compatibility.ondemand.session import ExplorationSession
from shader_deep.domain.blackboard import add_task
from shader_deep.domain.tasks import TaskRecord
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.domain.test_possibility_library import outline, report
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture


class IntegrationStaleTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="复刻参考图"))
        options = AnalysisOptions(max_integration_packages=3, max_integration_calls=3, max_request_retries=0)
        self.session = ExplorationSession(state, "A1", options, self.root, png_data_url(self.root / "reference.PNG"))
        self.session._submit_outline(outline(), ["平面组织", "空间形态"])
        for task in self.session._register():
            self.session._commit_report(task, report())
            self.session._collect(task, ExplorationOutcome(report=report(), execution=AnalysisExecution(status="completed"), issues=(), error=None))
        self.sketch_id = self.session.store.library.sketch_library[0].id
        self.session._read_library(
            {
                "comparison": {"question": "核对组成", "target_ids": [self.sketch_id]},
                "requests": [{"library": "sketch_library", "ids": [self.sketch_id]}],
            }
        )

    def _change_material(self, name: str, *, index: int = 0) -> None:
        store = self.session.store
        sketches = list(store.library.sketch_library)
        sketches[index] = replace(sketches[index], name=name)
        # 模拟另一个写入者的合法原子发布, 不伪造主模型的已呈现材料.
        store._commit(asdict(replace(store.library, sketch_library=tuple(sketches))), store.provenance, store.aliases)

    def _integrate(self) -> None:
        self.session._integrate(configure_analysis_model(build_model(), self.session.options))

    def test_stale_decision_refreshes_request_and_preserves_versioned_draft(self) -> None:
        versions: list[int] = []
        identities: list[str] = []
        revisions: list[int] = []

        def respond(request: dict[str, object]) -> dict[str, object]:
            group = self.session.comparisons.active
            if group is None:
                return self.call("finish_analysis", {})
            versions.append(group.version)
            identities.append(group.id)
            revisions.append(self.session.store.revision)
            self.assertEqual(set(group.entries), {self.sketch_id})
            if len(versions) == 1:
                self.assertNotIn("外部更新后的正文", json.dumps(request, ensure_ascii=False))
                self._change_material("外部更新后的正文")
            else:
                self.assertIn("外部更新后的正文", json.dumps(request, ensure_ascii=False))
                self.assertFalse(self.session.group_accepted)
                self.assertEqual(self.session.store.revision, revisions[0] + 1)
                self.assertEqual(self.session.last_action["status"], "materials_refreshed")
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        self._integrate()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(versions[1], versions[0] + 1)
        self.assertEqual(self.session.summary_result.status, "completed")
        drafts = [json.loads(path.read_text()) for path in sorted((self.root / "submissions").glob("*integration*.json"))]
        self.assertEqual(len(drafts), 2)
        first, refreshed = drafts
        self.assertEqual(first["arguments"], {"preserve": True})
        self.assertNotEqual(first.get("status"), "submitted")
        self.assertEqual(refreshed["status"], "submitted")
        self.assertNotEqual(first["draft_id"], refreshed["draft_id"])
        self.assertEqual(self.session.store.library.sketch_library[0].name, "外部更新后的正文")

    def test_continuous_material_changes_stop_at_group_budget(self) -> None:
        def respond(_request: dict[str, object]) -> dict[str, object]:
            self._change_material(f"不断变化的正文-{len(self.requests)}")
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        initial_revision = self.session.store.revision
        self._integrate()
        self.assertEqual(len(self.requests), self.session.options.max_integration_packages)
        self.assertEqual(self.session.store.revision, initial_revision + len(self.requests))
        self.assertEqual(self.session.summary_result.status, "partial")
        self.assertTrue(any(gap["reason"] == "integration_group_budget_exhausted" for gap in self.session.gaps))
        drafts = [json.loads(path.read_text()) for path in (self.root / "submissions").glob("*integration*.json")]
        self.assertEqual(len(drafts), self.session.options.max_integration_packages)
        self.assertTrue(all(draft.get("status") != "submitted" for draft in drafts))
        self.assertIsNotNone(self.session.comparisons.snapshot()["active"])

    def test_unrelated_material_change_does_not_refresh_or_expand_selection(self) -> None:
        versions: list[int] = []

        def respond(_request: dict[str, object]) -> dict[str, object]:
            group = self.session.comparisons.active
            if group is None:
                return self.call("finish_analysis", {})
            versions.append(group.version)
            self._change_material("其他条目外部更新", index=1)
            self.assertEqual(set(group.entries), {self.sketch_id})
            return self.call("submit_integration", {"preserve": True})

        self.response = respond
        self._integrate()
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.session.summary_result.status, "completed")
        self.assertFalse(self.session.comparisons.pending)
