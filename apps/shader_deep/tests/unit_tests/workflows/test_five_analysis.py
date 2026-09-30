"""无网络模型真实经过角色、工具、任务存储与五库发布链路."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import wait
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain_core.exceptions import ModelAPIError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from shader_deep.agents.five_analysis.agent import run_exploration
from shader_deep.infrastructure.llm.transport import RAW_TOOL_CALLS
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.five_analysis import FiveAnalysisResult, _recovery_input, _wait_workers, execute_five_analysis
from shader_deep.workflows.options import AnalysisOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import BaseMessage

    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.runtime.task_store import Attempt, JsonValue, Receipt


DIRECTIONS = ["检查轮廓", "检查色彩", "检查空间"]
REFERENCE = "data:image/png;base64,AA=="


def context(messages: list[BaseMessage]) -> dict[str, object]:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        blocks = message.content if isinstance(message.content, list) else [{"type": "text", "text": message.content}]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    payload = json.loads(str(block["text"]))
                except ValueError:
                    continue
                if isinstance(payload, dict) and ("user_request" in payload or "input_version" in payload):
                    return payload
    msg = "缺少真实请求上下文"
    raise AssertionError(msg)


def report() -> dict[str, object]:
    return {
        "features": [{"id": "F1", "description": "目标的圆形轮廓", "mechanism_refs": [{"id": "M1"}]}],
        "relations": [],
        "mechanisms": [{"id": "M1", "name": "圆形遮罩", "method": "从中心距离构造圆形遮罩"}],
        "sketches": [{"id": "S1", "element_id": "E1", "name": "默认方案", "default": {"F1": ["M1"]}, "composition": "用 [[M1]] 构造 [[F1]]"}],
    }


class RoutingFakeModel(BaseChatModel):
    """按实际角色材料响应, 并保留并发请求用于独立性断言."""

    requests: list[list[object]] = Field(default_factory=list)
    idle_direction: str | None = None
    idle_all: bool = False
    interrupt_direction: str | None = None
    permanent_direction: str | None = None
    recovery_retry: bool = True
    recovery_idle: bool = False
    integration_idle: bool = False
    integration_blind: bool = False
    merge: bool = False
    no_sketches: bool = False
    plan_count: int = 3
    invalid_target: bool = False
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _llm_type(self) -> str:
        return "five-analysis-test"

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> RoutingFakeModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object) -> ChatResult:
        del stop, run_manager, kwargs
        with self._lock:
            self.requests.append(list(messages))
            number = len(self.requests)
        payload = context(messages)
        prompt = str(messages[0].content)
        if "恢复决策主 agent" in prompt:
            if self.recovery_idle:
                message = AIMessage(content="需要考虑是否恢复", response_metadata={"finish_reason": "stop"})
            else:
                message = self._call(
                    "submit_recovery_decision",
                    {"decision": {"retry": self.recovery_retry, "reason": "依据当前任务错误决定保持输入重派或结束"}},
                    number,
                )
        elif "中心调度主" in prompt:
            plan = {
                "elements": [{"id": "E1", "name": "圆形", "region": "图中央的圆形主体", "feature_ids": []}],
                "target_element_id": "E1",
                "directions": DIRECTIONS[: self.plan_count],
            }
            message = self._call("submit_target_plan", {"plan": plan}, number)
        elif "独立探索 worker" in prompt:
            message = self._worker_response(payload, number)
        elif self.integration_blind:
            message = self._call("submit_merges", {"proposal": {"groups": []}}, number)
        elif self.integration_idle:
            message = AIMessage(content="继续整合", response_metadata={"finish_reason": "stop"})
        else:
            message = self._merge_response(messages, payload, number)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _worker_response(self, payload: dict[str, object], number: int) -> AIMessage:
        if payload["direction"] == self.permanent_direction:
            cause = RuntimeError("client closed")
            msg = "model connection failed"
            raise ModelAPIError(msg) from cause
        if payload["direction"] == self.interrupt_direction:
            raise KeyboardInterrupt
        if self.idle_all or payload["direction"] == self.idle_direction:
            return AIMessage(content="继续思考", response_metadata={"finish_reason": "stop"})
        value = report()
        if self.no_sketches:
            value["sketches"] = []
        if self.invalid_target:
            value["sketches"][0]["element_id"] = "E2"
        return self._call("submit_exploration", {"report": value}, number)

    def _merge_response(self, messages: list[BaseMessage], payload: dict[str, object], number: int) -> AIMessage:
        directory = payload["directory"]
        features = [item["id"] for item in directory["features"]]
        mechanisms = [item["id"] for item in directory["mechanisms"]]
        if not any(isinstance(message, ToolMessage) and message.name == "read_library_objects" for message in messages):
            return self._call("read_library_objects", {"ids": [*features, *mechanisms]}, number)
        if not self.merge:
            return self._call("submit_merges", {"proposal": {"groups": []}}, number)
        groups = [{"kind": "features", "ids": features, "keep": features[0]}, {"kind": "mechanisms", "ids": mechanisms, "keep": mechanisms[0]}]
        return self._call("submit_merges", {"proposal": {"groups": groups}}, number)

    @staticmethod
    def _call(name: str, arguments: dict[str, object], number: int) -> AIMessage:
        identifier = f"call-{number}"
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": arguments, "id": identifier}],
            additional_kwargs={RAW_TOOL_CALLS: [{"name": name, "args": json.dumps(arguments), "id": identifier}]},
            response_metadata={"finish_reason": "tool_calls"},
        )


class FiveAnalysisTests(unittest.TestCase):
    def run_case(self, model: RoutingFakeModel, options: AnalysisOptions | None = None) -> tuple[FiveAnalysisResult, Path]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        result = execute_five_analysis("分析圆形", REFERENCE, options or AnalysisOptions(), directory, model=model)
        return result, directory

    def test_real_roles_publish_union_and_independent_inputs(self) -> None:
        model = RoutingFakeModel()
        result, directory = self.run_case(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.selected_version, "V1")
        self.assertEqual(len(result.libraries.sketches), 3)
        self.assertEqual(len(result.libraries.features), 3)
        workers = [context(messages) for messages in model.requests if "独立探索 worker" in str(messages[0].content)]
        self.assertEqual({value["direction"] for value in workers}, set(DIRECTIONS))
        for value in workers:
            self.assertEqual(set(value), {"user_request", "target_element", "scope", "direction"})
            self.assertEqual(value["target_element"]["feature_ids"], [])
        with TaskStore(directory) as store:
            self.assertTrue(store.snapshot()["sealed"])
            self.assertEqual(store.read_version("V0")["libraries"]["sketches"], store.read_version("V1")["libraries"]["sketches"])

    def test_merge_rewrites_but_retains_all_sketches(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(merge=True))
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.libraries.features), 1)
        self.assertEqual(len(result.libraries.mechanisms), 1)
        self.assertEqual(len(result.libraries.sketches), 3)
        self.assertTrue(all(sketch.default == {"F1": ("M1",)} for sketch in result.libraries.sketches))
        with TaskStore(directory) as store:
            original = store.read_version("V0")["source_mappings"]
            final = store.read_version("V1")["source_mappings"]
            self.assertEqual(original["exploration-2"]["F1"], "F2")
            self.assertEqual(final["exploration-2"]["F1"], "F1")
            self.assertEqual(final["exploration-2"]["S1"], "S2")

    def test_integration_stall_falls_back_to_v0_with_recovery_bound(self) -> None:
        model = RoutingFakeModel(integration_idle=True)
        result, directory = self.run_case(model)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V0")
        self.assertIn("整合未完成", result.gaps[-1].description)
        with TaskStore(directory) as store:
            self.assertEqual(len(store.task("integration")["attempts"]), 3)
        integration = [messages for messages in model.requests if "整合工具人" in str(messages[0].content)]
        self.assertEqual(len(integration), 9)

    def test_failed_perspective_does_not_create_new_direction(self) -> None:
        model = RoutingFakeModel(idle_direction=DIRECTIONS[0])
        result, directory = self.run_case(model)
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.libraries.features), 2)
        with TaskStore(directory) as store:
            tasks = store.snapshot()["tasks"]
            self.assertEqual({key for key in tasks if key.startswith("exploration-")}, {"exploration-1", "exploration-2", "exploration-3"})
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 3)
        self.assertEqual(
            {context(messages)["direction"] for messages in model.requests if "独立探索 worker" in str(messages[0].content)}, set(DIRECTIONS)
        )

    def test_no_sketch_minimum_is_introduced(self) -> None:
        result, _ = self.run_case(RoutingFakeModel(no_sketches=True))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.libraries.sketches, ())

    def test_wrong_target_repairs_exhaust_without_baseline(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(invalid_target=True))
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.libraries)
        with TaskStore(directory) as store:
            self.assertEqual(store.task("exploration-1")["repairs"], 2)
            self.assertEqual(set(store.snapshot()["versions"]), {"final_projection"})

    def test_configured_two_perspectives_and_three_cap(self) -> None:
        result, _ = self.run_case(RoutingFakeModel(plan_count=2), AnalysisOptions(max_tasks=2))
        self.assertEqual(len(result.libraries.sketches), 2)
        result, _ = self.run_case(RoutingFakeModel(plan_count=2))
        self.assertEqual(result.status, "failed")

    def test_publish_failure_retains_immutable_baseline(self) -> None:
        original = TaskStore.publish_version

        def fail_v1(store: TaskStore, version: str, payload: JsonValue) -> str:
            if version == "V1":
                msg = "模拟发布失败"
                raise OSError(msg)
            return original(store, version, payload)

        with patch.object(TaskStore, "publish_version", fail_v1):
            result, directory = self.run_case(RoutingFakeModel())
        self.assertEqual(result.selected_version, "V0")
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertTrue(store.read_version("V0")["libraries"]["features"])

    def test_interrupt_delivers_v0_and_seals_before_propagating(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        delivered = []
        with (
            patch("shader_deep.workflows.five_analysis.run_integration", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            execute_five_analysis(
                "分析圆形",
                REFERENCE,
                AnalysisOptions(),
                directory,
                model=RoutingFakeModel(),
                on_delivery=lambda result, _store: delivered.append(result),
            )
        self.assertEqual(delivered[0].status, "partial")
        self.assertEqual(delivered[0].selected_version, "V0")
        with TaskStore(directory) as store:
            self.assertTrue(store.snapshot()["sealed"])
            self.assertEqual(store.task("integration")["status"], "cancelled")

    def test_integration_registration_failure_still_delivers_v0(self) -> None:
        original = TaskStore.register

        def failing(store: TaskStore, task_id: str, version: str, payload: JsonValue) -> None:
            if task_id == "integration":
                msg = "模拟整合登记失败"
                raise OSError(msg)
            original(store, task_id, version, payload)

        with patch.object(TaskStore, "register", failing):
            result, _ = self.run_case(RoutingFakeModel())
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V0")

    def test_worker_explicit_calls_survive_redispatch(self) -> None:
        model = RoutingFakeModel(idle_direction=DIRECTIONS[0])
        result, directory = self.run_case(model, AnalysisOptions(max_worker_calls=2))
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertEqual(store.task("exploration-1")["model_calls"], 2)
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 1)

    def test_completed_planning_quota_survives_coordinator_restart(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = "分析圆形"
        reference_digest = hashlib.sha256(REFERENCE.encode()).hexdigest()
        version = hashlib.sha256((request + REFERENCE).encode()).hexdigest()
        with TaskStore(directory) as store:
            store.register(
                "planning", version, {"user_request": request, "reference_path": "reference.png", "reference_fingerprint": reference_digest}
            )
            attempt = store.start_attempt("planning")
            store.consume_model_call(attempt, 1)
            store.submit(
                attempt,
                "planning-final",
                {
                    "elements": [{"id": "E1", "name": "圆形", "region": "图中央的圆形主体", "feature_ids": []}],
                    "target_element_id": "E1",
                    "directions": DIRECTIONS,
                },
            )
        model = RoutingFakeModel()
        result = execute_five_analysis(request, REFERENCE, AnalysisOptions(max_main_calls=1), directory, model=model)
        self.assertEqual(result.selected_version, "V0")
        self.assertEqual(result.status, "partial")
        self.assertFalse(any("整合工具人" in str(messages[0].content) for messages in model.requests))

    def test_empty_merge_without_reading_cannot_claim_completed(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(integration_blind=True))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V0")
        with TaskStore(directory) as store:
            self.assertEqual(store.task("integration")["repairs"], 2)
            self.assertEqual(store.task("integration")["status"], "failed")

    def test_exploration_interrupt_keeps_accepted_sibling_results(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        delivered = []
        accepted = threading.Event()
        finished = []
        lock = threading.Lock()

        def exploration(*args: object) -> None:
            if args[-1] == DIRECTIONS[0]:
                if not accepted.wait(5):
                    msg = "其他独立任务没有提交"
                    raise AssertionError(msg)
                raise KeyboardInterrupt
            run_exploration(*args)
            with lock:
                finished.append(args[-1])
                if len(finished) == len(DIRECTIONS) - 1:
                    accepted.set()

        with patch("shader_deep.workflows.five_analysis.run_exploration", side_effect=exploration), self.assertRaises(KeyboardInterrupt):
            execute_five_analysis(
                "分析圆形",
                REFERENCE,
                AnalysisOptions(),
                directory,
                model=RoutingFakeModel(),
                on_delivery=lambda result, _store: delivered.append(result),
            )
        self.assertEqual(delivered[0].status, "partial")
        self.assertEqual(delivered[0].selected_version, "V0")
        self.assertEqual(len(delivered[0].libraries.features), 2)
        with TaskStore(directory) as store:
            self.assertTrue(store.snapshot()["sealed"])
            self.assertEqual(store.task("exploration-1")["status"], "cancelled")

    def test_permanent_model_configuration_error_does_not_redispatch(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(permanent_direction=DIRECTIONS[0]))
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 1)
            self.assertEqual(store.task("exploration-1")["model_calls"], 1)

    def test_interrupt_projection_survives_prepared_package_seal_failure(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        prepared = []
        with (
            patch("shader_deep.workflows.five_analysis.run_integration", side_effect=KeyboardInterrupt),
            patch.object(TaskStore, "seal", side_effect=OSError("模拟封存写入失败")),
            self.assertRaises(OSError),
        ):
            execute_five_analysis(
                "分析圆形",
                REFERENCE,
                AnalysisOptions(),
                directory,
                model=RoutingFakeModel(),
                on_delivery=lambda result, _store: prepared.append(result),
            )
        model = RoutingFakeModel()
        restored = execute_five_analysis("分析圆形", REFERENCE, AnalysisOptions(), directory, model=model)
        self.assertEqual(restored.gaps, prepared[0].gaps)
        self.assertEqual(restored.libraries, prepared[0].libraries)
        self.assertEqual(restored.selected_version, prepared[0].selected_version)
        self.assertEqual(model.requests, [])
        with TaskStore(directory) as store:
            self.assertTrue(store.snapshot()["sealed"])

    def test_main_thread_interrupt_revokes_worker_before_shutdown_wait(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        interrupted = threading.Event()
        observed = []

        def worker(attempt: Attempt, _execution: AnalysisExecution) -> None:
            interrupted.wait(2)
            # 必须在线程池退出等待期间撤销权限, 不让终止依赖工作者先结束.
            for _ in range(100):
                if store.task(attempt.task_id)["status"] == "cancelled":
                    break
                threading.Event().wait(0.001)
            observed.append(store.is_active(attempt))

        def stop_wait(*_args: object, **_kwargs: object) -> None:
            interrupted.set()
            raise KeyboardInterrupt

        with TaskStore(directory) as store:
            store.register("one", "fixed", {})
            with patch("shader_deep.workflows.five_analysis.wait", side_effect=stop_wait), self.assertRaises(KeyboardInterrupt):
                _wait_workers(store, {"one": worker}, 1)
            self.assertEqual(observed, [False])
            self.assertEqual(store.task("one")["status"], "cancelled")

    def test_projection_recovery_rejects_changed_business_input(self) -> None:
        _, directory = self.run_case(RoutingFakeModel())
        with self.assertRaisesRegex(ValueError, "输入不可改变"):
            execute_five_analysis("换一个要求", REFERENCE, AnalysisOptions(), directory, model=RoutingFakeModel())

    def test_main_recovery_can_decline_third_attempt_with_independent_input(self) -> None:
        model = RoutingFakeModel(idle_direction=DIRECTIONS[0], recovery_retry=False)
        result, directory = self.run_case(model)
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 2)
            self.assertEqual(store.task("recovery-exploration-1")["status"], "succeeded")
            decision = store.read_result("recovery-exploration-1")
            self.assertFalse(decision["retry"])
            self.assertIn(decision["reason"], store.task("exploration-1")["error"])
        recoveries = [context(messages) for messages in model.requests if "恢复决策主 agent" in str(messages[0].content)]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            set(recoveries[0]), {"original_task_id", "input_version", "failed_attempt_id", "error", "user_request", "target", "direction", "scope"}
        )
        self.assertEqual(recoveries[0]["direction"], DIRECTIONS[0])
        self.assertEqual(recoveries[0]["target"]["feature_ids"], [])

    def test_failed_recovery_role_has_no_recursive_recovery(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(idle_direction=DIRECTIONS[0], recovery_idle=True))
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 2)
            self.assertEqual(len(store.task("recovery-exploration-1")["attempts"]), 1)
            self.assertEqual(store.task("recovery-exploration-1")["model_calls"], 3)
            self.assertFalse(any(task_id.startswith("recovery-recovery-") for task_id in store.snapshot()["tasks"]))

    def test_parallel_recovery_roles_share_main_quota_atomically(self) -> None:
        model = RoutingFakeModel(idle_all=True)
        result, directory = self.run_case(model, AnalysisOptions(max_main_calls=2))
        self.assertEqual(result.status, "failed")
        with TaskStore(directory) as store:
            tasks = store.snapshot()["tasks"]
            total = sum(task["model_calls"] for task_id, task in tasks.items() if task_id == "planning" or task_id.startswith("recovery-"))
            self.assertEqual(total, 2)
            self.assertEqual(sum(len(tasks[f"exploration-{number}"]["attempts"]) for number in range(1, 4)), 7)
        self.assertEqual(sum("恢复决策主 agent" in str(messages[0].content) for messages in model.requests), 1)

    def test_recovery_cost_reduces_shared_integration_quota(self) -> None:
        result, directory = self.run_case(RoutingFakeModel(idle_direction=DIRECTIONS[0]), AnalysisOptions(max_main_calls=3))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V0")
        with TaskStore(directory) as store:
            self.assertEqual(store.task("planning")["model_calls"], 1)
            self.assertEqual(store.task("recovery-exploration-1")["model_calls"], 1)
            self.assertEqual(store.task("integration")["model_calls"], 1)
            self.assertEqual(store.task("integration")["status"], "failed")

    def test_main_interrupt_cancels_recovery_before_waiting_for_worker_exit(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recovery_started = threading.Event()
        interrupted = threading.Event()
        observed = []
        delivered = []
        original_wait = wait

        def recovery(store: TaskStore, attempt: Attempt, *_args: object) -> None:
            recovery_started.set()
            interrupted.wait(2)
            deadline = time.monotonic() + 1
            while store.task(attempt.task_id)["status"] != "cancelled" and time.monotonic() < deadline:
                threading.Event().wait(0.001)
            observed.append(store.is_active(attempt))

        def main_wait(*args: object, **kwargs: object) -> tuple[set[object], set[object]]:
            if recovery_started.wait(0.001):
                interrupted.set()
                raise KeyboardInterrupt
            return original_wait(*args, **kwargs)

        with (
            patch("shader_deep.workflows.five_analysis.run_recovery", side_effect=recovery),
            patch("shader_deep.workflows.five_analysis.wait", side_effect=main_wait),
            self.assertRaises(KeyboardInterrupt),
        ):
            execute_five_analysis(
                "分析圆形",
                REFERENCE,
                AnalysisOptions(),
                directory,
                model=RoutingFakeModel(idle_direction=DIRECTIONS[0]),
                on_delivery=lambda result, _store: delivered.append(result),
            )
        self.assertEqual(observed, [False])
        self.assertEqual(delivered[0].status, "partial")
        with TaskStore(directory) as store:
            self.assertEqual(store.task("recovery-exploration-1")["status"], "cancelled")
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 2)

    def test_successful_recovery_decision_reused_after_coordinator_restart(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = "分析圆形"
        version = hashlib.sha256((request + REFERENCE).encode()).hexdigest()
        digest = hashlib.sha256(REFERENCE.encode()).hexdigest()
        element = {"id": "E1", "name": "圆形", "region": "图中央的圆形主体", "feature_ids": []}
        with TaskStore(directory) as store:
            store.register("planning", version, {"user_request": request, "reference_path": "reference.png", "reference_fingerprint": digest})
            planning = store.start_attempt("planning")
            store.consume_model_call(planning)
            store.submit(planning, "plan", {"elements": [element], "target_element_id": "E1", "directions": DIRECTIONS})
            store.register(
                "exploration-1",
                version,
                {
                    "user_request": request,
                    "reference_path": "reference.png",
                    "reference_fingerprint": digest,
                    "target": element,
                    "direction": DIRECTIONS[0],
                },
            )
            for error in ("初次失败", "自动重派失败"):
                attempt = store.start_attempt("exploration-1")
                store.fail_attempt(attempt, error)
            store.register("recovery-exploration-1", version, _recovery_input(store, "exploration-1"))
            recovery = store.start_attempt("recovery-exploration-1")
            store.consume_model_call(recovery)
            store.submit(recovery, "decision", {"retry": True, "reason": "保持相同输入进行最后一次重派"})
        model = RoutingFakeModel()
        result = execute_five_analysis(request, REFERENCE, AnalysisOptions(max_main_calls=4), directory, model=model)
        self.assertEqual(result.status, "completed")
        self.assertFalse(any("恢复决策主 agent" in str(messages[0].content) for messages in model.requests))
        with TaskStore(directory) as store:
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 3)
            self.assertEqual(store.task("recovery-exploration-1")["model_calls"], 1)

    def test_parent_cancelled_during_recovery_registration_prevents_new_request(self) -> None:
        original = TaskStore.register

        def cancelled_parent(store: TaskStore, task_id: str, version: str, payload: JsonValue) -> None:
            if task_id.startswith("recovery-"):
                store.cancel(task_id.removeprefix("recovery-"), "主线程在恢复登记窗口取消")
            original(store, task_id, version, payload)

        model = RoutingFakeModel(idle_direction=DIRECTIONS[0])
        with patch.object(TaskStore, "register", cancelled_parent):
            result, directory = self.run_case(model)
        self.assertEqual(result.status, "partial")
        self.assertFalse(any("恢复决策主 agent" in str(messages[0].content) for messages in model.requests))
        with TaskStore(directory) as store:
            self.assertEqual(store.task("recovery-exploration-1")["model_calls"], 0)
            self.assertEqual(store.task("recovery-exploration-1")["status"], "cancelled")
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 2)

    def test_parent_cancelled_before_recovery_submit_rejects_child_result(self) -> None:
        original = TaskStore.submit

        def cancelled_parent(
            store: TaskStore,
            attempt: Attempt,
            identity: str,
            payload: JsonValue,
            *,
            parent_task_id: str | None = None,
        ) -> Receipt:
            if parent_task_id is not None:
                store.cancel(parent_task_id, "原任务在恢复提交前取消")
            return original(store, attempt, identity, payload, parent_task_id=parent_task_id)

        with patch.object(TaskStore, "submit", cancelled_parent):
            result, directory = self.run_case(RoutingFakeModel(idle_direction=DIRECTIONS[0]))
        self.assertEqual(result.status, "partial")
        with TaskStore(directory) as store:
            self.assertNotEqual(store.task("recovery-exploration-1")["status"], "succeeded")
            self.assertIsNone(store.task("recovery-exploration-1")["result_path"])
            self.assertEqual(len(store.task("exploration-1")["attempts"]), 2)
