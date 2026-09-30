import json
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from shader_deep.runtime.task_store import Attempt, JsonValue, TaskStore, files


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = TaskStore(self.directory).__enter__()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.store.__exit__, None, None, None)

    def attempt(self) -> Attempt:
        self.store.register("explore-1", "reference-v1", {"target": "E1"})
        return self.store.start_attempt("explore-1")

    def test_result_survives_missing_notification_and_restart(self) -> None:
        attempt = self.attempt()
        receipt = self.store.submit(attempt, "submission-1", {"features": ["F1"]})
        self.store.__exit__(None, None, None)
        with TaskStore(self.directory) as resumed:
            self.assertEqual(resumed.task("explore-1")["status"], "succeeded")
            self.assertEqual(resumed.read_result("explore-1"), {"features": ["F1"]})
            self.assertEqual(resumed.submit(attempt, "submission-1", {"features": ["F1"]}), receipt)

    def test_duplicate_success_after_seal_returns_original_without_writing(self) -> None:
        attempt = self.attempt()
        receipt = self.store.submit(attempt, "submission-1", {"ok": True})
        self.store.seal(package_path="report")
        state = self.store.snapshot()
        with patch.object(files, "immutable_file", side_effect=OSError("storage unavailable")):
            self.assertEqual(self.store.submit(attempt, "submission-1", {"ok": True}), receipt)
        self.assertEqual(self.store.snapshot(), state)
        with self.assertRaises(ValueError):
            self.store.submit(attempt, "submission-1", {"ok": False})
        self.assertEqual(self.store.read_result("explore-1"), {"ok": True})

    def test_changed_identity_payload_is_rejected_after_success(self) -> None:
        attempt = self.attempt()
        self.store.submit(attempt, "submission-1", {"ok": True})
        with self.assertRaises(ValueError):
            self.store.submit(attempt, "submission-1", {"ok": False})
        with self.assertRaises(ValueError):
            self.store.submit(replace(attempt, task_id="other"), "submission-1", {"ok": True})

    def test_cancel_rejects_late_result_and_audits_without_result_pointer(self) -> None:
        attempt = self.attempt()
        self.store.cancel("explore-1", "用户取消")
        with self.assertRaises(ValueError):
            self.store.submit(attempt, "late", {"ok": True})
        self.assertEqual(self.store.task("explore-1")["status"], "cancelled")
        self.assertIsNone(self.store.task("explore-1")["result_path"])
        audit = json.loads((self.directory / "late-submissions.jsonl").read_text())
        self.assertEqual(audit["submission_id"], "late")

    def test_audit_io_failure_does_not_change_prior_commit(self) -> None:
        attempt = self.attempt()
        self.store.cancel("explore-1", "停止")
        state = self.store.snapshot()
        with patch.object(Path, "open", side_effect=OSError("disk full")), self.assertRaisesRegex(ValueError, "拒绝新写入"):
            self.store.submit(attempt, "late", {"ok": True})
        self.assertEqual(self.store.snapshot(), state)

    def test_cancel_and_submit_publish_only_one_legal_terminal_state(self) -> None:
        attempt = self.attempt()
        barrier = threading.Barrier(2)
        failures: list[Exception] = []

        def submit() -> None:
            barrier.wait()
            try:
                self.store.submit(attempt, "racing", {"ok": True})
            except ValueError as error:
                failures.append(error)

        worker = threading.Thread(target=submit)
        worker.start()
        barrier.wait()
        self.store.cancel("explore-1", "取消")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        snapshot = self.store.snapshot()
        self.assertEqual(json.loads((self.directory / "commit.json").read_text()), snapshot)
        if self.store.task("explore-1")["status"] == "succeeded":
            self.assertFalse(failures)
            self.assertEqual(self.store.read_result("explore-1"), {"ok": True})
        else:
            self.assertEqual(self.store.task("explore-1")["status"], "cancelled")
            self.assertEqual(len(failures), 1)
            self.assertEqual(snapshot["receipts"], {})

    def test_repair_count_is_shared_across_attempts_and_persisted(self) -> None:
        first = self.attempt()
        self.store.consume_repair(first)
        self.store.fail_attempt(first, "格式错误")
        second = self.store.start_attempt("explore-1")
        self.store.consume_repair(second)
        self.store.fail_attempt(second, "格式错误")
        self.store.__exit__(None, None, None)
        with TaskStore(self.directory) as resumed:
            third = resumed.start_attempt("explore-1")
            self.assertEqual(resumed.task("explore-1")["repairs"], 2)
            with self.assertRaises(ValueError):
                resumed.consume_repair(third)
            resumed.fail_attempt(third, "修复耗尽")
            self.assertEqual(resumed.task("explore-1")["status"], "failed")
            with self.assertRaises(ValueError):
                resumed.start_attempt("explore-1")

    def test_replaced_attempt_cannot_submit(self) -> None:
        first = self.attempt()
        self.store.fail_attempt(first, "无进展")
        second = self.store.start_attempt("explore-1")
        with self.assertRaises(ValueError):
            self.store.submit(first, "stale", {"ok": True})
        self.assertTrue(self.store.is_active(second))
        self.store.submit(second, "current", {"ok": True})

    def test_input_version_and_generation_are_enforced(self) -> None:
        attempt = self.attempt()
        for changed in (
            replace(attempt, input_version="different"),
            replace(attempt, generation=attempt.generation + 1),
            replace(attempt, attempt_id="unknown"),
        ):
            with self.assertRaises(ValueError):
                self.store.submit(changed, "invalid", {})
        self.assertTrue(self.store.is_active(attempt))

    def test_takeover_interrupts_running_attempt_without_resetting_counts(self) -> None:
        first = self.attempt()
        self.store.consume_repair(first)
        self.store.__exit__(None, None, None)
        with TaskStore(self.directory) as resumed:
            task = resumed.task("explore-1")
            self.assertEqual(task["status"], "pending")
            attempts = cast("list[dict[str, JsonValue]]", task["attempts"])
            self.assertEqual(attempts[0]["status"], "interrupted")
            self.assertEqual(task["repairs"], 1)
            second = resumed.start_attempt("explore-1")
            self.assertGreater(second.generation, first.generation)
            with self.assertRaises(ValueError):
                resumed.submit(first, "late", {})
            resumed.submit(second, "accepted", {})

    def test_takeover_of_third_attempt_finishes_failed(self) -> None:
        attempt = self.attempt()
        for _ in range(2):
            self.store.fail_attempt(attempt, "失败")
            attempt = self.store.start_attempt("explore-1")
        self.store.__exit__(None, None, None)
        with TaskStore(self.directory) as resumed:
            self.assertEqual(resumed.task("explore-1")["status"], "failed")
            with self.assertRaises(ValueError):
                resumed.start_attempt("explore-1")

    def test_process_lock_blocks_live_coordinator(self) -> None:
        script = (
            "from pathlib import Path\n"
            "from shader_deep.runtime.task_store import TaskStore\n"
            "import sys\n"
            "try:\n"
            "    with TaskStore(Path(sys.argv[1])): pass\n"
            "except BlockingIOError:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        # 子进程仅执行测试内固定脚本, 参数来自本测试创建的临时目录.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script, str(self.directory)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_commit_leaves_memory_disk_and_result_acceptance_unchanged(self) -> None:
        attempt = self.attempt()
        old = self.store.snapshot()
        committed = (self.directory / "commit.json").read_bytes()
        writer = files.atomic_write

        def fail_commit(path: Path, data: bytes) -> None:
            if path.name == "commit.json":
                msg = "commit failed"
                raise OSError(msg)
            writer(path, data)

        with patch.object(files, "atomic_write", side_effect=fail_commit), self.assertRaises(OSError):
            self.store.submit(attempt, "submission", {"ok": True})
        self.assertEqual(self.store.snapshot(), old)
        self.assertEqual((self.directory / "commit.json").read_bytes(), committed)
        with self.assertRaises(ValueError):
            self.store.read_result("explore-1")
        self.store.submit(attempt, "submission", {"ok": True})
        self.assertEqual(self.store.task("explore-1")["status"], "succeeded")

    def test_result_write_failure_never_accepts_success(self) -> None:
        attempt = self.attempt()
        state = self.store.snapshot()
        with patch.object(files, "immutable_file", side_effect=OSError("write failed")), self.assertRaises(OSError):
            self.store.submit(attempt, "submission", {})
        self.assertEqual(self.store.snapshot(), state)
        self.assertTrue(self.store.is_active(attempt))

    def test_failed_version_publish_preserves_v0_and_rejects_overwrite(self) -> None:
        baseline = {"features": ["F1", "F2"]}
        path = self.store.publish_version("V0", baseline)
        with patch.object(files, "atomic_write", side_effect=OSError("publish failed")), self.assertRaises(OSError):
            self.store.publish_version("V1", {"features": ["F1"]})
        self.assertEqual(self.store.read_version("V0"), baseline)
        self.assertEqual(self.store.publish_version("V0", baseline), path)
        with self.assertRaises(ValueError):
            self.store.publish_version("V0", {})
        with self.assertRaises(KeyError):
            self.store.read_version("V1")

    def test_seal_records_package_and_prevents_new_writes(self) -> None:
        attempt = self.attempt()
        self.store.seal(package_path="report")
        self.assertEqual(self.store.snapshot()["package_path"], "report")
        self.assertFalse(self.store.is_active(attempt))
        self.assertEqual(self.store.task("explore-1")["status"], "cancelled")
        self.store.seal(package_path="report")
        with self.assertRaises(ValueError):
            self.store.seal(package_path="other")
        with self.assertRaises(ValueError):
            self.store.register("new", "v1", {})
        with self.assertRaises(ValueError):
            self.store.submit(attempt, "late", {})

    def test_permanent_failure_is_terminal(self) -> None:
        attempt = self.attempt()
        self.store.fail_attempt(attempt, "权限配置错误", permanent=True)
        self.assertEqual(self.store.task("explore-1")["status"], "failed")
        self.store.cancel("explore-1", "取消")
        self.assertEqual(self.store.task("explore-1")["status"], "failed")
        with self.assertRaises(ValueError):
            self.store.start_attempt("explore-1")

    def test_registration_and_reads_do_not_share_mutable_data(self) -> None:
        payload: dict[str, JsonValue] = {"items": ["E1"]}
        self.store.register("task", "v1", payload)
        cast("list[JsonValue]", payload["items"]).append("E2")
        task = self.store.task("task")
        copied = cast("dict[str, JsonValue]", task["payload"])
        cast("list[JsonValue]", copied["items"]).append("E3")
        self.assertEqual(self.store.task("task")["payload"], {"items": ["E1"]})
        self.store.register("task", "v1", {"items": ["E1"]})
        with self.assertRaises(ValueError):
            self.store.register("task", "v1", {"items": ["E2"]})

    def test_non_json_payloads_are_rejected(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        for payload in (float("nan"), float("inf"), {1: "x"}, ("x",), cyclic):
            with self.subTest(payload_type=type(payload)), self.assertRaises(ValueError):
                self.store.register("task", "v1", cast("JsonValue", payload))

    def test_model_calls_persist_across_attempts_and_coordinator_restart(self) -> None:
        first = self.attempt()
        self.assertEqual(self.store.consume_model_call(first, limit=2), 1)
        self.store.fail_attempt(first, "暂时故障")
        second = self.store.start_attempt("explore-1")
        self.assertEqual(self.store.consume_model_call(second, limit=2), 2)
        self.store.__exit__(None, None, None)
        with TaskStore(self.directory) as resumed:
            third = resumed.start_attempt("explore-1")
            self.assertEqual(resumed.task("explore-1")["model_calls"], 2)
            with self.assertRaisesRegex(ValueError, "调用额度已耗尽"):
                resumed.consume_model_call(third, limit=2)
            self.assertEqual(resumed.task("explore-1")["model_calls"], 2)

    def test_model_call_commit_failure_does_not_consume_or_send(self) -> None:
        attempt = self.attempt()
        state = self.store.snapshot()
        with (
            patch.object(files, "atomic_write", side_effect=OSError("commit failed")),
            self.assertRaises(OSError),
        ):
            self.store.consume_model_call(attempt, limit=1)
        self.assertEqual(self.store.snapshot(), state)
        self.assertEqual(self.store.consume_model_call(attempt, limit=1), 1)

    def test_unlimited_calls_still_persist_and_require_live_instance(self) -> None:
        attempt = self.attempt()
        for count in range(1, 5):
            self.assertEqual(self.store.consume_model_call(attempt), count)
        self.store.cancel("explore-1", "停止")
        with self.assertRaises(ValueError):
            self.store.consume_model_call(attempt)
        self.assertEqual(self.store.task("explore-1")["model_calls"], 4)

    def test_valid_json_tampering_is_rejected_for_results_and_versions(self) -> None:
        attempt = self.attempt()
        receipt = self.store.submit(attempt, "accepted", {"features": ["F1"]})
        version_path = self.store.publish_version("V0", {"features": ["F2"]})
        state = self.store.snapshot()
        (self.directory / receipt.result_path).write_text('{"features":["changed"]}')
        (self.directory / version_path).write_text('{"features":["changed"]}')
        with self.assertRaisesRegex(ValueError, "提交指纹不一致"):
            self.store.read_result("explore-1")
        with self.assertRaisesRegex(ValueError, "提交指纹不一致"):
            self.store.read_version("V0")
        self.assertEqual(self.store.snapshot(), state)

    def recovery_attempt(self) -> Attempt:
        self.store.register("original", "v1", {})
        self.store.register("recovery-original", "v1", {"original_task_id": "original"})
        return self.store.start_attempt("recovery-original")

    def test_parent_cancel_fences_new_recovery_call_and_submission_atomically(self) -> None:
        recovery = self.recovery_attempt()
        self.store.cancel("original", "用户取消")
        state = self.store.snapshot()
        self.assertTrue(self.store.is_active(recovery))
        with self.assertRaisesRegex(ValueError, "原任务不再等待恢复"):
            self.store.consume_model_call(recovery, parent_task_id="original")
        with self.assertRaisesRegex(ValueError, "原任务不再等待恢复"):
            self.store.submit(recovery, "decision", {"retry": True}, parent_task_id="original")
        self.assertEqual(self.store.snapshot(), state)

    def test_successful_recovery_replay_precedes_parent_cancel_fence(self) -> None:
        recovery = self.recovery_attempt()
        receipt = self.store.submit(recovery, "decision", {"retry": True}, parent_task_id="original")
        self.store.cancel("original", "用户取消")
        self.store.seal()
        self.assertEqual(self.store.submit(recovery, "decision", {"retry": True}, parent_task_id="original"), receipt)

    def test_parent_cancel_and_recovery_submit_have_one_serialized_outcome(self) -> None:
        recovery = self.recovery_attempt()
        barrier = threading.Barrier(2)
        failures: list[Exception] = []

        def submit() -> None:
            barrier.wait()
            try:
                self.store.submit(recovery, "decision", {"retry": True}, parent_task_id="original")
            except ValueError as error:
                failures.append(error)

        worker = threading.Thread(target=submit)
        worker.start()
        barrier.wait()
        self.store.cancel("original", "用户取消")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.task("original")["status"], "cancelled")
        if self.store.task("recovery-original")["status"] == "succeeded":
            self.assertFalse(failures)
            self.assertEqual(self.store.read_result("recovery-original"), {"retry": True})
        else:
            self.assertEqual(len(failures), 1)
            self.assertIsNone(self.store.task("recovery-original")["result_path"])
            self.assertEqual(self.store.snapshot()["receipts"], {})


if __name__ == "__main__":
    unittest.main()
