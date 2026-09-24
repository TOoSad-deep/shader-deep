"""提交草稿只修正失败字段, 且不绕过业务校验或重复登记."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from unittest import TestCase
from unittest.mock import Mock, patch

from langchain.agents.middleware.types import ToolCallRequest
from langchain.messages import HumanMessage
from langchain.tools import tool
from pydantic import BaseModel, ConfigDict

from shader_deep.domain.legacy import LensReport  # noqa: TC001  # 工具装饰器在运行时解析报告类型.
from shader_deep.domain.validation import AnalysisValidationError
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler, resolve_pointer


class ExampleReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation: str
    count: int
    refs: list[str]


class SubmissionTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.accepted: list[dict[str, object]] = []
        self.events: list[tuple[str, dict[str, object]]] = []

        @tool
        def submit_analysis_report(report: ExampleReport, summary: str) -> str:
            """提交测试报告, 真实调用由共用入口完成."""
            return report.observation + summary

        def submit(arguments: dict[str, object]) -> str:
            if arguments["report"].refs != ["O1"]:
                raise AnalysisValidationError([{"path": "/report/refs/0", "code": "invalid_reference", "message": "Use O1"}])
            self.accepted.append(arguments)
            return json.dumps({"status": "submitted"})

        self.submit_tool = submit_analysis_report
        self.handler = SubmissionHandler(
            "task-1",
            self.submit_tool,
            submit,
            lambda: json.dumps({"status": "already_submitted"}) if self.accepted else None,
            Lock(),
            directory=self.directory,
        )
        self.loop = AnalysisLoop(
            AnalysisExecution(),
            3,
            lambda: HumanMessage(content="reference"),
            lambda: bool(self.accepted),
            [self.submit_tool, self.handler.repair_tool()],
            submission_handler=self.handler,
            on_event=lambda name, details: self.events.append((name, details)),
        )

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        request = ToolCallRequest(
            tool_call={"name": name, "args": arguments, "id": "call-1", "type": "tool_call"}, tool=None, state={}, runtime=Mock()
        )
        fallback = Mock(side_effect=AssertionError("Submission must bypass ordinary schema rejection"))
        reply = self.loop.wrap_tool_call(request, fallback)
        return json.loads(reply.content)

    def repair(self, revision: int, changes: list[dict[str, object]]) -> dict[str, object]:
        return self.call("repair_analysis_submission", {"draft_id": "task-1-submission", "expected_revision": revision, "changes": changes})

    def test_schema_then_business_failure_can_be_repaired_without_rewriting_other_fields(self) -> None:
        reply = self.call(
            "submit_analysis_report", {"report": {"observation": "preserve", "count": "wrong", "refs": ["H1"]}, "summary": "keep outer text"}
        )
        self.assertEqual(reply["revision"], 1)
        self.assertEqual(reply["errors"][0]["path"], "/report/count")
        self.assertIn("business checks have not run", reply["message"])
        self.assertEqual(reply["remaining_errors"], 0)
        self.assertFalse(self.accepted)
        reply = self.repair(1, [{"op": "set", "path": "/report/count", "value": 2}])
        self.assertEqual(reply["revision"], 2)
        self.assertEqual(reply["errors"][0]["path"], "/report/refs/0")
        self.assertFalse(self.accepted)
        reply = self.repair(2, [{"op": "set", "path": "/report/refs/0", "value": "O1"}])
        self.assertEqual(reply["status"], "submitted")
        self.assertEqual(self.accepted[0]["report"].observation, "preserve")
        self.assertEqual(self.accepted[0]["summary"], "keep outer text")
        saved = json.loads((self.directory / "submissions/task-1.json").read_text())
        self.assertEqual(saved["status"], "submitted")
        self.assertEqual(saved["revision"], 3)
        saved_events = [details for name, details in self.events if name == "submission_saved"]
        self.assertEqual([event["accepted"] for event in saved_events], [False, False, True])
        self.assertEqual(
            [event["invoked_tool"] for event in saved_events], ["submit_analysis_report", "repair_analysis_submission", "repair_analysis_submission"]
        )
        self.assertTrue(all(event["submission_tool"] == "submit_analysis_report" for event in saved_events))

    def test_failed_patch_is_atomic_and_a_new_submission_invalidates_old_revision(self) -> None:
        arguments = {"report": {"observation": "preserve", "count": "wrong", "refs": ["H1"]}, "summary": "outer"}
        self.call("submit_analysis_report", arguments)
        original = deepcopy(self.handler.draft)
        reply = self.repair(1, [{"op": "set", "path": "/report/count", "value": 2}, {"op": "remove", "path": "/report/missing"}])
        self.assertEqual(reply["status"], "invalid_submission")
        self.assertEqual(reply["errors"][0]["path"], "/changes/1/path")
        self.assertIn("/report/missing", reply["errors"][0]["message"])
        self.assertEqual(self.handler.draft, original)
        self.assertEqual(len([event for event in self.events if event[0] == "submission_saved"]), 1)
        self.call("submit_analysis_report", arguments)
        current = deepcopy(self.handler.draft)
        reply = self.repair(1, [{"op": "set", "path": "/report/refs", "value": ["O1"]}])
        self.assertEqual(reply["errors"][0]["path"], "/expected_revision")
        reply = self.repair(2, [{"op": "set", "path": "/report/refs"}])
        self.assertEqual(reply["errors"][0]["path"], "/changes/0/value")
        reply = self.repair(2, [{"op": "set", "path": "", "value": []}])
        self.assertEqual(reply["errors"][0]["path"], "/changes/0/value")
        reply = self.repair(2, [])
        self.assertEqual(reply["errors"][0]["path"], "/changes")
        self.assertEqual(self.handler.draft, current)
        self.assertFalse(self.accepted)

    def test_success_guards_later_full_submission_and_patch_before_mutating_draft(self) -> None:
        arguments = {"report": {"observation": "preserve", "count": 1, "refs": ["O1"]}, "summary": "outer"}
        handle = self.handler.handle
        barrier = Barrier(2)

        def simultaneous(name: str, parameters: dict[str, object]) -> object:
            barrier.wait(timeout=5)
            return handle(name, parameters)

        with patch.object(self.handler, "handle", side_effect=simultaneous), ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda _: self.call("submit_analysis_report", arguments), range(2)))
        self.assertEqual(sorted(reply["status"] for reply in replies), ["already_submitted", "submitted"])
        original = deepcopy(self.handler.draft)
        self.assertEqual(self.call("submit_analysis_report", {"malformed": True})["status"], "already_submitted")
        self.assertEqual(self.repair(1, [{"op": "remove", "path": "/report"}])["status"], "already_submitted")
        self.assertEqual(self.handler.draft, original)
        self.assertEqual(len(self.accepted), 1)
        saved_events = [details for name, details in self.events if name == "submission_saved"]
        self.assertEqual(len(saved_events), 1)
        self.assertEqual(saved_events[0]["invoked_tool"], "submit_analysis_report")
        self.assertTrue(saved_events[0]["accepted"])

    def test_nested_report_reference_errors_keep_all_patchable_paths(self) -> None:
        @tool
        def submit_report(report: LensReport, summary: str) -> str:
            """验证真实报告的嵌套校验反馈."""
            return report.scope + summary

        handler = SubmissionHandler("nested", submit_report, lambda _args: '{"status":"submitted"}', lambda: None, Lock())
        report = {
            "scope": "all",
            "observations": [{"id": "O1", "text": "visible", "region": "all"}],
            "interpretations": [
                {"id": name, "text": "hypothesis", "supporting_observation_ids": ["O1"], "opposing_observation_ids": ["O1"]} for name in ("H1", "H2")
            ],
        }
        rejected = json.loads(handler.handle("submit_report", {"report": report, "summary": "keep"}).content)
        paths = [f"/report/interpretations/{index}/opposing_observation_ids" for index in range(2)]
        self.assertEqual([error["path"] for error in rejected["errors"]], paths)
        repaired = handler.handle(
            "repair_analysis_submission",
            {"draft_id": rejected["draft_id"], "expected_revision": 1, "changes": [{"op": "set", "path": path, "value": []} for path in paths]},
        )
        self.assertEqual(json.loads(repaired.content)["status"], "submitted")

    def test_pointer_escaping_and_remove_work_without_implicit_array_append(self) -> None:
        arguments = {"report": {"observation": "preserve", "count": "wrong", "refs": ["O1"]}, "summary": "outer", "a/b": {"~key": 1}}
        self.call("submit_analysis_report", arguments)
        self.assertEqual(resolve_pointer(arguments, "/a~1b/~0key"), 1)
        current = deepcopy(self.handler.draft)
        self.repair(1, [{"op": "set", "path": "/report/refs/1", "value": "O2"}])
        self.assertEqual(self.handler.draft, current)
        self.repair(1, [{"op": "remove", "path": "/a~1b/~0key"}])
        self.assertEqual(self.handler.draft["arguments"]["a/b"], {})

    def test_task_ids_cannot_escape_or_collide_in_submission_storage(self) -> None:
        run = self.directory / "run"
        run.mkdir()
        outside = self.directory / "existing.json"
        outside.write_text("preserve", encoding="utf-8")
        identifiers = ("../../existing", str(outside.with_suffix("")), "a/b", "a_b", "分析任务", "x" * 300)
        for identifier in identifiers:
            with self.subTest(task_id=identifier):
                handler = SubmissionHandler(identifier, self.submit_tool, lambda _args: "submitted", lambda: None, Lock(), directory=run)
                rejected = handler.handle("submit_analysis_report", {"report": {}, "summary": "keep"})
                self.assertEqual(json.loads(rejected.content)["status"], "invalid_submission")
                repaired = handler.handle(
                    "repair_analysis_submission",
                    {
                        "draft_id": handler.snapshot()["draft_id"],
                        "expected_revision": 1,
                        "changes": [{"op": "set", "path": "/report", "value": {"observation": "visible", "count": 1, "refs": ["O1"]}}],
                    },
                )
                self.assertEqual(repaired.content, "submitted")
                self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
        files = list((run / "submissions").iterdir())
        saved = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        self.assertEqual(len(files), len(identifiers))
        self.assertEqual({item["task_id"] for item in saved}, set(identifiers))
        for item in saved:
            self.assertEqual(item["revision"], 2)
            self.assertEqual(item["status"], "submitted")
