"""持续主 Agent 的业务动作保护中间登记、恢复、版本与真实交付边界."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.agents.five_analysis.agent import _charge_model
from shader_deep.domain.five_libraries import Element, ExplorationSubmission
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError
from shader_deep.runtime.task_store import TaskStore
from shader_deep.runtime.task_store.types import object_value
from shader_deep.workflows.five_analysis import MainAnalysisActions, _exception_result, execute_five_analysis
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.workflows.test_five_analysis import DIRECTIONS, REFERENCE, RoutingFakeModel, report

if TYPE_CHECKING:
    from shader_deep.runtime.task_store import Attempt, JsonValue, Receipt


ELEMENT = Element(id="E1", name="圆形", region="图中央的圆形主体", feature_ids=())


class MainActionsTests(unittest.TestCase):
    def actions(self, model: RoutingFakeModel | None = None, options: AnalysisOptions | None = None) -> MainAnalysisActions:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = self.enterContext(TaskStore(directory))
        store.register("main", "fixed", {})
        return MainAnalysisActions(
            store=store,
            model=model or RoutingFakeModel(),
            options=options or AnalysisOptions(),
            request="分析圆形",
            reference_url=REFERENCE,
            input_version="fixed",
            attempt=store.start_attempt("main"),
        )

    def dispatch(self, actions: MainAnalysisActions) -> dict[str, JsonValue]:
        actions.submit_elements((ELEMENT,))
        return actions.dispatch_exploration("E1", tuple(DIRECTIONS))

    def test_intermediate_acceptance_keeps_main_active_and_unfinished(self) -> None:
        actions = self.actions()
        initial = actions.progress()
        self.assertNotIn("dispatch_exploration", actions.allowed_tools())
        receipt = actions.submit_elements((ELEMENT,))
        self.assertEqual(receipt["elements"], [ELEMENT.model_dump(mode="json")])
        self.assertTrue(actions.store.is_active(actions.attempt))
        self.assertFalse(actions.done())
        self.assertNotEqual(initial, actions.progress())
        self.assertIn("dispatch_exploration", actions.allowed_tools())
        self.assertEqual(actions.store.task("elements")["model_calls"], 0)

    def test_invalid_element_catalogs_leave_no_accepted_state(self) -> None:
        actions = self.actions()
        cases = ((), (ELEMENT, ELEMENT), (ELEMENT.model_copy(update={"feature_ids": ("F1",)}),))
        for elements in cases:
            with self.subTest(elements=elements), self.assertRaisesRegex(ValueError, "元素必须"):
                actions.submit_elements(elements)
        self.assertNotIn("elements", object_value(actions.store.snapshot()["tasks"]))
        self.assertNotIn("dispatch_exploration", actions.allowed_tools())

    def test_bad_references_and_configured_count_do_not_create_workers(self) -> None:
        actions = self.actions()
        with self.assertRaises(ValueError):
            actions.dispatch_exploration("E1", tuple(DIRECTIONS))
        actions.submit_elements((ELEMENT,))
        for target, directions in (("E2", tuple(DIRECTIONS)), ("E1", tuple(DIRECTIONS[:2]))):
            with self.subTest(target=target, directions=directions), self.assertRaises(ValueError):
                actions.dispatch_exploration(target, directions)
        self.assertFalse(any(task.startswith("exploration-") for task in object_value(actions.store.snapshot()["tasks"])))

    def test_single_batch_replay_preserves_input_attempts_and_calls(self) -> None:
        actions = self.actions()
        first = self.dispatch(actions)
        state = actions.store.snapshot()
        with patch("shader_deep.workflows.five_analysis._wait_workers", side_effect=AssertionError("重放不应启动线程池")):
            replay = actions.dispatch_exploration("E1", tuple(DIRECTIONS))
        self.assertEqual(first, replay)
        self.assertEqual(state, actions.store.snapshot())
        with self.assertRaisesRegex(ValueError, "单批探索"):
            actions.dispatch_exploration("E1", ("新方向", *DIRECTIONS[1:]))
        with self.assertRaisesRegex(ValueError, "不可改写"):
            actions.submit_elements((ELEMENT.model_copy(update={"region": "其他区域"}),))

    def test_main_serial_action_keeps_worker_batch_parallel(self) -> None:
        actions = self.actions(options=AnalysisOptions(max_parallel=3))
        barrier = threading.Barrier(3, timeout=5)
        entered: list[str] = []
        mutex = threading.Lock()

        def worker(store: TaskStore, attempt: Attempt, _model: object, _options: object, _execution: AnalysisExecution, *_inputs: object) -> None:
            with mutex:
                entered.append(attempt.task_id)
            barrier.wait()
            store.submit(attempt, attempt.task_id + "-test", report())

        with patch("shader_deep.workflows.five_analysis.run_exploration", worker):
            receipt = self.dispatch(actions)
        self.assertEqual(set(entered), {"exploration-1", "exploration-2", "exploration-3"})
        self.assertEqual(len(receipt["result_ids"]), 3)

    def test_internal_recovery_returns_final_status_with_fixed_direction(self) -> None:
        model = RoutingFakeModel(idle_direction=DIRECTIONS[0], recovery_retry=False)
        actions = self.actions(model)
        receipt = self.dispatch(actions)
        tasks = {object_value(item)["task_id"]: object_value(item) for item in receipt["tasks"]}
        self.assertEqual(tasks["exploration-1"]["status"], "cancelled")
        self.assertIsNone(tasks["exploration-1"]["result_id"])
        self.assertEqual(len(actions.store.task("exploration-1")["attempts"]), 2)
        self.assertEqual(actions.store.task("exploration-1")["payload"]["direction"], DIRECTIONS[0])
        self.assertTrue(actions.store.is_active(actions.attempt))

    def test_pending_exploration_prevents_baseline_and_integration(self) -> None:
        actions = self.actions()
        actions.submit_elements((ELEMENT,))
        with patch("shader_deep.workflows.five_analysis._wait_workers"):
            actions.dispatch_exploration("E1", tuple(DIRECTIONS))
        with self.assertRaisesRegex(ValueError, "尚未结束"):
            actions.dispatch_integration()
        self.assertNotIn("V0", object_value(actions.store.snapshot()["versions"]))
        self.assertNotIn("integration", object_value(actions.store.snapshot()["tasks"]))

    def test_pending_recovery_prevents_integration_even_with_accepted_reports(self) -> None:
        actions = self.actions()
        self.dispatch(actions)
        actions.store.register("recovery-exploration-1", "fixed", {})
        with self.assertRaisesRegex(ValueError, "recovery-exploration-1"):
            actions.dispatch_integration()
        self.assertNotIn("V0", object_value(actions.store.snapshot()["versions"]))

    def test_baseline_mapping_and_failed_source_gap_survive_integration(self) -> None:
        actions = self.actions(RoutingFakeModel(idle_direction=DIRECTIONS[0], recovery_retry=False, merge=True))
        self.dispatch(actions)
        receipt = actions.dispatch_integration()
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["selected_version"], "V1")
        self.assertTrue(any("exploration-1" in object_value(gap)["description"] for gap in receipt["gaps"]))
        baseline = object_value(actions.store.read_version("V0"))
        final = object_value(actions.store.read_version("V1"))
        self.assertEqual(baseline["source_mappings"]["exploration-3"]["F1"], "F2")
        self.assertEqual(final["source_mappings"]["exploration-3"]["F1"], "F1")
        self.assertEqual(len(final["libraries"]["sketches"]), 2)
        self.assertEqual(baseline["libraries"]["sketches"][1]["id"], final["libraries"]["sketches"][1]["id"])

    def test_no_baseline_returns_failed_without_starting_integration(self) -> None:
        actions = self.actions(RoutingFakeModel(idle_all=True, recovery_retry=False))
        self.dispatch(actions)
        receipt = actions.dispatch_integration()
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(receipt["selected_version"])
        self.assertNotIn("integration", object_value(actions.store.snapshot()["tasks"]))
        self.assertFalse({"V0", "V1"} & set(object_value(actions.store.snapshot()["versions"])))
        self.assertEqual(actions.finish_analysis()["status"], "failed")

    def test_integration_failure_retains_v0_and_repeated_call_is_cached(self) -> None:
        actions = self.actions(RoutingFakeModel(integration_blind=True))
        self.dispatch(actions)
        receipt = actions.dispatch_integration()
        state = actions.store.snapshot()
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["selected_version"], "V0")
        with patch("shader_deep.workflows.five_analysis._integrate", side_effect=AssertionError("缓存不得重复执行整合")):
            self.assertEqual(receipt, actions.dispatch_integration())
        self.assertEqual(state, actions.store.snapshot())

    def test_early_end_is_failed_or_partial_and_rejects_later_actions(self) -> None:
        actions = self.actions()
        actions.submit_elements((ELEMENT,))
        self.assertEqual(actions.finish_analysis()["status"], "failed")
        self.assertTrue(actions.done())
        with self.assertRaises(AnalysisLimitError):
            actions.dispatch_exploration("E1", tuple(DIRECTIONS))
        actions = self.actions()
        self.dispatch(actions)
        receipt = actions.finish_analysis()
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["selected_version"], "V0")
        self.assertNotIn("integration", object_value(actions.store.snapshot()["tasks"]))

    def test_early_end_revokes_pending_workers_and_keeps_failures(self) -> None:
        actions = self.actions()
        actions.submit_elements((ELEMENT,))
        with patch("shader_deep.workflows.five_analysis._wait_workers"):
            actions.dispatch_exploration("E1", tuple(DIRECTIONS))
        self.assertEqual(actions.finish_analysis()["status"], "failed")
        self.assertTrue(all(actions.store.task(f"exploration-{number}")["status"] == "cancelled" for number in range(1, 4)))

    def test_only_accepted_business_results_are_readable(self) -> None:
        actions = self.actions()
        self.dispatch(actions)
        before = actions.progress()
        receipt = actions.read_analysis_result("exploration-1")
        self.assertEqual(receipt["result"], ExplorationSubmission.model_validate(report()).model_dump(mode="json"))
        self.assertNotEqual(before, actions.progress())
        for result_id in ("../../commit.json", "main", "planning", "recovery-exploration-1", "V1"):
            with self.subTest(result_id=result_id), self.assertRaises(ValueError):
                actions.read_analysis_result(result_id)

    def test_program_side_submit_failure_does_not_leave_orphan_running(self) -> None:
        actions = self.actions()
        original = TaskStore.submit

        def fail_elements(store: TaskStore, attempt: Attempt, identity: str, payload: JsonValue, **kwargs: object) -> Receipt:
            if attempt.task_id == "elements":
                msg = "模拟元素提交故障"
                raise OSError(msg)
            return original(store, attempt, identity, payload, **kwargs)

        with patch.object(TaskStore, "submit", fail_elements), self.assertRaisesRegex(OSError, "模拟元素提交故障"):
            actions.submit_elements((ELEMENT,))
        self.assertEqual(actions.store.task("elements")["status"], "failed")
        self.assertTrue(actions.store.is_active(actions.attempt))

    def test_main_quota_failure_after_integration_keeps_accepted_v1(self) -> None:
        actions = self.actions(options=AnalysisOptions(max_main_calls=2))
        self.dispatch(actions)
        self.assertEqual(actions.dispatch_integration()["selected_version"], "V1")
        with self.assertRaises(AnalysisLimitError) as caught:
            _charge_model(actions.store, actions.attempt, actions.options, "outline")
        result = _exception_result(actions.store, caught.exception)
        self.assertEqual(result.selected_version, "V1")
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.libraries.features), 3)
        self.assertIn("主 Agent", result.gaps[-1].description)

    def test_main_model_budget_exhaustion_after_integrating_keeps_v1_on_replay(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        result = execute_five_analysis("分析圆形", REFERENCE, AnalysisOptions(max_main_calls=9), directory, model=RoutingFakeModel())
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V1")
        self.assertEqual(len(result.libraries.features), 3)
        with TaskStore(directory) as store:
            self.assertEqual(store.task("main")["status"], "failed")
            self.assertEqual(store.task("integration")["status"], "succeeded")
            self.assertEqual(object_value(store.read_version("integration_projection"))["status"], "completed")
        model = RoutingFakeModel()
        restored = execute_five_analysis("分析圆形", REFERENCE, AnalysisOptions(max_main_calls=9), directory, model=model)
        self.assertEqual(restored, result)
        self.assertEqual(model.requests, [])

    def test_restart_uses_persisted_catalog_and_integration_without_rerun(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with TaskStore(directory) as store:
            store.register("main", "fixed", {})
            actions = MainAnalysisActions(
                store=store,
                model=RoutingFakeModel(),
                options=AnalysisOptions(),
                request="分析圆形",
                reference_url=REFERENCE,
                input_version="fixed",
                attempt=store.start_attempt("main"),
            )
            self.dispatch(actions)
            receipt = actions.dispatch_integration()
        with TaskStore(directory) as store:
            actions = MainAnalysisActions(
                store=store,
                model=RoutingFakeModel(),
                options=AnalysisOptions(),
                request="分析圆形",
                reference_url=REFERENCE,
                input_version="fixed",
                attempt=store.start_attempt("main"),
            )
            self.assertEqual(actions.context()["registered_elements"], [ELEMENT.model_dump(mode="json")])
            with patch("shader_deep.workflows.five_analysis._integrate", side_effect=AssertionError("恢复不能重跑整合")):
                self.assertEqual(receipt, actions.dispatch_integration())
            self.assertEqual(actions.finish_analysis()["status"], "completed")
            self.assertEqual(store.task("main")["status"], "succeeded")
            self.assertEqual(len(store.task("integration")["attempts"]), 1)

    def test_finish_recovers_v1_published_before_integration_projection(self) -> None:
        original = TaskStore.publish_version
        for failed_direction in (None, DIRECTIONS[0]):
            with self.subTest(failed_direction=failed_direction):
                directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

                def stop_projection(store: TaskStore, version: str, payload: JsonValue) -> str:
                    if version == "integration_projection":
                        msg = "模拟 V1 后投影发布前中断"
                        raise OSError(msg)
                    return original(store, version, payload)

                with TaskStore(directory) as store:
                    store.register("main", "fixed", {})
                    actions = MainAnalysisActions(
                        store=store,
                        model=RoutingFakeModel(merge=True, idle_direction=failed_direction, recovery_retry=False),
                        options=AnalysisOptions(),
                        request="分析圆形",
                        reference_url=REFERENCE,
                        input_version="fixed",
                        attempt=store.start_attempt("main"),
                    )
                    self.dispatch(actions)
                    with patch.object(TaskStore, "publish_version", stop_projection), self.assertRaisesRegex(OSError, "投影发布前中断"):
                        actions.dispatch_integration()
                    final = object_value(store.read_version("V1"))
                    self.assertNotIn("integration_projection", object_value(store.snapshot()["versions"]))
                    self.assertEqual(len(final["libraries"]["features"]), 1)
                with TaskStore(directory) as store:
                    model = RoutingFakeModel()
                    actions = MainAnalysisActions(
                        store=store,
                        model=model,
                        options=AnalysisOptions(),
                        request="分析圆形",
                        reference_url=REFERENCE,
                        input_version="fixed",
                        attempt=store.start_attempt("main"),
                    )
                    with patch("shader_deep.workflows.five_analysis._integrate", side_effect=AssertionError("结束不得重跑已成功整合")):
                        receipt = actions.finish_analysis()
                    self.assertEqual(receipt["selected_version"], "V1")
                    self.assertEqual(receipt["status"], "partial" if failed_direction else "completed")
                    self.assertEqual(receipt["gaps"], final["gaps"])
                    self.assertEqual(receipt["open_questions"], final["open_questions"])
                    self.assertEqual(len(store.task("integration")["attempts"]), 1)
                    self.assertEqual(model.requests, [])

    def test_exception_after_v1_publication_before_projection_keeps_v1(self) -> None:
        actions = self.actions(RoutingFakeModel(merge=True))
        self.dispatch(actions)
        original = TaskStore.publish_version

        def stop_projection(store: TaskStore, version: str, payload: JsonValue) -> str:
            if version == "integration_projection":
                msg = "模拟投影发布失败"
                raise OSError(msg)
            return original(store, version, payload)

        with patch.object(TaskStore, "publish_version", stop_projection), self.assertRaises(OSError) as caught:
            actions.dispatch_integration()
        result = _exception_result(actions.store, caught.exception)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V1")
        self.assertEqual(len(result.libraries.features), 1)
        self.assertEqual(actions.store.task("integration")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
