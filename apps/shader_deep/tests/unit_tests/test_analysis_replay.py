"""冻结探索回放保留完成义务, 不继承旧整合状态或再次请求探索."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from unittest.mock import patch

from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.exploration_worker import ExplorationOutcome, OutlineIssue
from shader_deep.analysis.replay import load_replay_input, prepare_replay, run_replay
from shader_deep.analysis.types import AnalysisExecution, AnalysisOptions
from shader_deep.blackboard import add_task
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TaskRecord
from tests.unit_tests._generation_fixture import GenerationFixture
from tests.unit_tests.test_possibility_library import outline, report


class AnalysisReplayTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "reference.png").write_bytes((self.root / "reference.PNG").read_bytes())
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="比较外观"))
        session = ExplorationSession(state, "A1", AnalysisOptions(), self.source, png_data_url(self.source / "reference.png"))
        session._submit_outline(outline(), ["平面组织", "空间形态", "光照"])
        tasks = session._register()
        for task in tasks[:2]:
            session._commit_report(task, report())
            session._collect(
                task, ExplorationOutcome(report=report(), execution=AnalysisExecution(status="completed", model_calls=5), issues=(), error=None)
            )
        issue = OutlineIssue(region="全图", description="初稿遗漏", element_ids=(outline().elements[0].id,))
        session._collect(
            tasks[2],
            ExplorationOutcome(
                report=None,
                execution=AnalysisExecution(status="failed", model_calls=2, error="provider failed"),
                issues=(issue,),
                error="provider failed",
            ),
        )
        self.report_ids = session.store.report_ids
        self.original_library = session.store.library
        sketches = list(session.store.library.sketch_library)
        sketches[0] = replace(sketches[0], name="旧整合已修改名称")
        session.store._commit(asdict(replace(session.store.library, sketch_library=tuple(sketches))), session.store.provenance, session.store.aliases)
        session.store.presented.add(sketches[0].id)
        session.comparisons.start(session.store, "旧判断", [sketches[0].id])
        session.execution.model_calls = 9
        session.execution.format_repair_calls = 2
        session.execution.format_repair_scopes = {"outline": 2}
        session.resolutions = {"1": {"disposition": "dismissed"}}
        session.finalize()
        session.save()
        self.source_bytes = (self.source / "run.json").read_bytes()
        self.options = AnalysisOptions(output_dir=self.root / "replays", max_main_calls=4, integration_max_output_tokens=10000)

    def test_restore_initial_library_with_issues_failures_and_stable_report_ids(self) -> None:
        session = prepare_replay(self.source, options=self.options)
        self.assertEqual(session.store.report_ids, self.report_ids)
        self.assertEqual(session.store.library, self.original_library)
        self.assertEqual(session.execution.model_calls, 0)
        self.assertEqual(session.execution.format_repair_calls, 0)
        self.assertEqual(session.execution.format_repair_scopes, {})
        self.assertEqual(session.store.presented, set())
        self.assertEqual(session.store.aliases, {})
        self.assertEqual(session.comparisons.snapshot()["works"], [])
        self.assertEqual(session.resolutions, {})
        self.assertEqual(len(session.issues), 1)
        self.assertEqual(len(session.workers), 3)
        failed = [worker for worker in session.workers.values() if worker.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error, "provider failed")
        self.assertTrue(all(worker.model_calls == 0 for worker in session.workers.values()))
        self.assertIsNone(session.summary_result)
        manifest = json.loads((session.directory / "replay-manifest.json").read_text())
        self.assertEqual(manifest["source_run_sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertEqual(manifest["effective_integration_output"]["max_output_tokens"], 10000)
        self.assertIn("analysis/replay.py", manifest["source_fingerprints"])
        self.assertEqual((self.source / "run.json").read_bytes(), self.source_bytes)
        self.assertEqual((session.directory / "reference.png").read_bytes(), (self.source / "reference.png").read_bytes())

    def test_finish_keeps_failed_task_and_unresolved_issue_partial_without_exploration(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            names = {tool["function"]["name"] for tool in request["tools"]}
            if "review_outline" in names:
                return self.call("review_outline", {"decisions": [{"issue_id": "1", "disposition": "deferred", "reason": "仍缺观察证据"}]})
            return self.call("plan_comparisons", {"comparisons": [], "reason": "本轮没有必要合并"})

        self.response = respond
        with patch.object(ExplorationSession, "_run_batch", side_effect=AssertionError("must not explore")):
            outcome = run_replay(self.source, options=self.options)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertTrue(self.requests)
        saved = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(saved["main_execution"]["model_calls"], len(self.requests))
        self.assertEqual(len(saved["issues"]), 1)
        self.assertEqual(saved["issue_resolutions"]["1"]["disposition"], "deferred")
        self.assertEqual((self.source / "run.json").read_bytes(), self.source_bytes)

    def test_hash_mismatch_rejected_before_creating_replay(self) -> None:
        (self.source / "reference.png").write_bytes((self.source / "reference.png").read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "hash"):
            prepare_replay(self.source, options=self.options)
        self.assertFalse(self.options.output_dir.exists())

    def test_missing_frozen_worker_is_not_silently_dropped(self) -> None:
        payload = json.loads(self.source_bytes)
        payload["worker_executions"].pop(next(iter(payload["worker_executions"])))
        (self.source / "run.json").write_text(json.dumps(payload))
        with self.assertRaises(KeyError):
            load_replay_input(self.source)

    def test_service_failure_preserves_error_and_original_inputs(self) -> None:
        with patch("shader_deep.analysis.replay.build_model", side_effect=RuntimeError("fixture unavailable")):
            outcome = run_replay(self.source, options=self.options)
        self.assertEqual(outcome.stop_reason, "error")
        saved = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertIn("fixture unavailable", saved["main_execution"]["error"])
        self.assertEqual(saved["main_execution"]["model_calls"], 0)
        self.assertEqual(len(saved["issues"]), 1)
        self.assertEqual(outcome.summary_result.status, "partial")

    def test_programmatic_replay_rejects_unbounded_model_calls(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive main-call"):
            run_replay(self.source, options=replace(self.options, max_main_calls=0))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.options.output_dir.exists())
