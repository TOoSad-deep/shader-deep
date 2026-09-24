"""目录历史整理保留完整调用配对、未消费回执与有效修复依据."""

from __future__ import annotations

import json
from unittest import TestCase

from langchain.messages import AIMessage, ToolMessage

from shader_deep.analysis.context_views import bounded_context_page
from shader_deep.analysis.history import compact_consumed_history


class AnalysisHistoryTests(TestCase):
    def test_context_page_keeps_large_identity_discoverable_and_binds_cursor(self) -> None:
        rows = [{"id": "issue-1", "detail": "large" * 1000}, {"id": "issue-2", "detail": "small"}]
        page = bounded_context_page("issues", rows, limit=1, max_chars=500)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 500)
        self.assertEqual(page["entries"], [{"id": "issue-1", "details_unavailable_due_to_budget": True}])
        following = bounded_context_page("issues", rows, cursor=page["next_cursor"], max_chars=500)
        self.assertEqual(following["entries"], [rows[1]])
        self.assertIsNone(following["next_cursor"])
        with self.assertRaisesRegex(ValueError, "Stale"):
            bounded_context_page("failed", rows, cursor=page["next_cursor"], max_chars=500)
        rows[0]["detail"] = "changed"
        with self.assertRaisesRegex(ValueError, "Stale"):
            bounded_context_page("issues", rows, cursor=page["next_cursor"], max_chars=500)

    def test_consumed_pages_shrink_but_latest_page_remains_complete(self) -> None:
        messages = []
        for identity in ("old", "new"):
            messages += [
                AIMessage(content="", tool_calls=[{"name": "list_library", "args": {"limit": 20}, "id": identity}]),
                ToolMessage(content=json.dumps({"entries": ["large" * 1000], "next_cursor": identity, "remaining": 8}), tool_call_id=identity),
            ]
        result = compact_consumed_history(messages, {"old"})
        self.assertLess(len(result[1].content), 1000)
        self.assertEqual(result[3], messages[3])
        self.assertEqual(result[0].tool_calls[0]["id"], result[1].tool_call_id)
        self.assertEqual(json.loads(result[1].content)["next_cursor"], "old")
        self.assertGreater(len(messages[1].content), 5000)

    def test_only_successful_read_receipts_are_compacted(self) -> None:
        messages = []
        for identity, status in (("ok", "selected"), ("failed", "not_selected")):
            messages += [
                AIMessage(content="", tool_calls=[{"name": "read_library", "args": {}, "id": identity}]),
                ToolMessage(content=json.dumps({"status": status, "selected_ids": ["long" * 1000]}), tool_call_id=identity),
            ]
        result = compact_consumed_history(messages, {"ok", "failed"})
        self.assertLess(len(result[1].content), 1000)
        self.assertEqual(result[3], messages[3])

    def test_draft_requires_explicit_replacement_and_complete_pair(self) -> None:
        call = {"name": "submit_integration", "args": {"decision": "full draft"}, "id": "draft"}
        messages = [
            AIMessage(content="", tool_calls=[call], additional_kwargs={"tool_calls": [call]}),
            ToolMessage(content="/merges/2 invalid", tool_call_id="draft", status="error"),
        ]
        self.assertEqual(compact_consumed_history(messages, {"draft"}), messages)
        result = compact_consumed_history(messages, set(), replaced_draft_call_ids={"draft"})
        self.assertEqual(result[0].tool_calls[0]["args"], {})
        self.assertEqual(result[0].tool_calls[0]["id"], result[1].tool_call_id)
        self.assertNotIn("tool_calls", result[0].additional_kwargs)
        self.assertTrue(json.loads(result[1].content)["current_draft_in_context"])
        self.assertEqual(compact_consumed_history(messages[:1], set(), replaced_draft_call_ids={"draft"}), messages[:1])
