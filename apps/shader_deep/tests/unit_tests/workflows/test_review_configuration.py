"""评审配置的预算校验与路径覆盖行为."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shader_deep.workflows.review.configuration import apply_review_overrides, load_review_options
from shader_deep.workflows.review.options import ReviewOptions


class ReviewConfigurationTests(unittest.TestCase):
    def test_defaults_and_explicit_override_preserve_relative_yaml_path(self) -> None:
        self.assertEqual(load_review_options(), ReviewOptions())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.yaml"
            path.write_text("max_model_calls: 7\noutput_dir: reports\n", encoding="utf-8")
            options = load_review_options(path)
            self.assertEqual(options.output_dir, path.parent.resolve() / "reports")
            self.assertEqual(apply_review_overrides(options, {"max_model_calls": 2}).max_model_calls, 2)
            self.assertEqual(options.max_model_calls, 7)

    def test_invalid_parameters_are_rejected_before_execution(self) -> None:
        for field, value in (
            ("max_model_calls", True),
            ("max_model_calls", 0),
            ("max_request_retries", -1),
            ("request_timeout_seconds", 0),
            ("max_output_tokens", 1.5),
            ("stream_model_responses", 1),
            ("output_dir", "reports"),
            ("unknown", 1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                apply_review_overrides(ReviewOptions(), {field: value})

    def test_configuration_requires_a_mapping_with_known_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.yaml"
            for content in ("[]\n", "unknown: 1\n", "max_model_calls: true\n", "output_dir: ''\n", "1: value\n"):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises((TypeError, ValueError)):
                    load_review_options(path)
