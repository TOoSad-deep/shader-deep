"""终端过滤、日志故障隔离与脱敏边界的可观察行为."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from shader_deep.infrastructure.analysis_logging import LOGGER, analysis_console, log_analysis, submission_summary


class AnalysisLoggingTests(TestCase):
    def test_main_intermediate_submissions_have_readable_element_and_dispatch_summaries(self) -> None:
        elements = {"elements": [{"id": "E1", "name": "蓝青色发光框", "region": "图中央"}]}
        self.assertIn("元素 E1: 蓝青色发光框; 范围=图中央", submission_summary("submit_elements", elements))
        dispatch = {"target_element_id": "E1", "directions": ["检查轮廓", "检查色彩"]}
        summary = submission_summary("dispatch_exploration", dispatch)
        self.assertIn("目标=E1", summary)
        self.assertIn("探索方向 2: 检查色彩", summary)

    def test_diagnostics_redact_sensitive_fields_and_leave_business_results_visible(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = io.StringIO()
            with analysis_console(output, "DEBUG"):
                log_analysis(
                    directory,
                    "探索结果",
                    details={
                        "name": "蓝青色发光框",
                        "nested": {"api_key": "private-key", "reasoning_details": [{"text": "private-thought"}]},
                        "image": "data:image/png;base64,QUJDRA==",
                        "authorization": "Bearer private-token",
                    },
                )
            for text in (output.getvalue(), (directory / "analysis.log").read_text()):
                self.assertIn("蓝青色发光框", text)
                self.assertIn("[REDACTED]", text)
                for sensitive in ("private-key", "private-thought", "QUJDRA==", "private-token"):
                    self.assertNotIn(sensitive, text)

    def test_file_failure_still_prints_progress_and_restores_host_logger(self) -> None:
        previous_level, previous_propagation = LOGGER.level, LOGGER.propagate
        previous_handlers = list(LOGGER.handlers)
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "analysis.log").mkdir()
            output = io.StringIO()
            with analysis_console(output, "INFO"):
                log_analysis(directory, "元素拆分完成", task_id="planning")
                log_analysis(directory, "完整提交", level=logging.DEBUG, details={"elements": []})
            self.assertIn("无法写入分析文本日志", output.getvalue())
            self.assertIn("元素拆分完成", output.getvalue())
            self.assertNotIn("完整提交", output.getvalue())
        self.assertEqual(LOGGER.level, previous_level)
        self.assertEqual(LOGGER.propagate, previous_propagation)
        self.assertEqual(LOGGER.handlers, previous_handlers)
