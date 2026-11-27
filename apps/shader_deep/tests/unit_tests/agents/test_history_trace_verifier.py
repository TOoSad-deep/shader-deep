"""离线保护轻量追踪核验, 将预算阻止的回调与 HTTP 尝试分开."""

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from langsmith import Client

from scripts.verify_history_display_traces import SELECT, verify, verify_target

HTTP_LIMIT = 18


def run(identifier: str, *, ended: bool = True, error: bool = False, kind: str = "llm") -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        name="run",
        run_type=kind,
        trace_id="root",
        parent_run_id=None,
        start_time=datetime(2026, 11, 6, tzinfo=UTC),
        end_time=datetime(2026, 11, 6, tzinfo=UTC) if ended else None,
        error="private-error-content" if error else None,
        session_id="session",
    )


class HistoryTraceVerifierTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = Mock(spec=Client)
        self.client.get_run_url.return_value = "https://example.invalid/run/root"

    def test_blocked_nineteenth_callback_is_not_an_http_attempt_or_count_failure(self) -> None:
        events = [{"kind": "model_start"}] * 19 + [{"kind": "model_error"}, {"kind": "http_budget_blocked"}]
        (self.directory / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events))
        self.client.list_runs.side_effect = [
            [run("root", error=True, kind="chain")],
            (run(str(index), error=index == HTTP_LIMIT) for index in range(19)),
        ]
        record = {"trace": {"run_id": "root"}, "status": "failed", "http_requests": [{}] * 18, "http_attempts": 18}
        result = verify_target(self.client, record, self.directory, identifier="A", project="shader-deep", judge=False)
        self.assertEqual(result["verification_status"], "terminal")
        self.assertEqual((result["http_attempts"], result["callback_model_starts"], result["llm_runs"]), (18, 19, 19))
        self.assertTrue(result["llm_runs_match_callback_starts"])
        self.assertFalse(result["llm_runs_match_http_attempts"])
        self.assertEqual(result["http_budget_blocked_events"], 1)
        self.assertEqual(result["llm_error_runs"], 1)
        self.assertNotIn("private-error-content", json.dumps(result))

    def test_iterator_is_not_truncated_at_one_hundred_and_select_excludes_payloads(self) -> None:
        self.client.list_runs.side_effect = [[run("root", kind="chain")], (run(str(index)) for index in range(121))]
        record = {"trace": {"run_id": "root"}, "http_requests": [{}] * 121}
        result = verify_target(self.client, record, self.directory, identifier="A", project="shader-deep", judge=False)
        self.assertEqual(result["llm_runs"], 121)
        self.assertIsNone(result["callback_model_starts"])
        for call in self.client.list_runs.call_args_list:
            self.assertEqual(call.kwargs["select"], SELECT)
            self.assertNotIn("limit", call.kwargs)
        self.assertFalse({"inputs", "outputs", "events", "extra"} & set(SELECT))
        self.client.read_run.assert_not_called()

    def test_missing_pending_and_query_error_remain_distinct(self) -> None:
        record = {"trace": {"run_id": "root"}, "http_requests": []}
        for responses, expected in (
            ([[]], "missing_root"),
            ([[run("root", ended=False)], [run("child", ended=False)]], "pending"),
            (RuntimeError("sensitive error"), "query_error"),
        ):
            with self.subTest(expected=expected):
                self.client.list_runs.side_effect = responses
                result = verify_target(self.client, record, self.directory, identifier="A", project="shader-deep", judge=False)
                self.assertEqual(result["verification_status"], expected)
                self.assertNotIn("sensitive error", json.dumps(result))

    def test_suite_includes_missing_tuning_cases_and_judge_llm_root_without_modifying_receipts(self) -> None:
        (self.directory / "suite.json").write_text(json.dumps({"cases": [{"id": f"R{index}"} for index in range(6)]}))
        judge = self.directory / "judges" / "sample-01"
        judge.mkdir(parents=True)
        receipt = json.dumps({"trace_id": "judge-root", "status": "completed", "requests": [{}], "untouched": "keep"})
        (judge / "summary.json").write_text(receipt)
        self.client.list_runs.side_effect = [[run("judge-root")], [run("judge-root")]]
        result = verify(self.directory, self.client)
        self.assertEqual(result["local_tuning_cases"], 6)
        self.assertEqual(result["status_counts"], {"missing_local_summary": 6, "terminal": 1})
        final = result["checks"][-1]
        self.assertEqual(final["kind"], "judge")
        self.assertTrue(final["judge_single_llm_root"])
        self.assertEqual(final["http_attempts"], 1)
        self.assertEqual((judge / "summary.json").read_text(), receipt)
