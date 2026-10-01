"""确保模型、服务地址和凭据来自同一配置组."""

from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from shader_deep.infrastructure.llm.client import build_model

LEGACY = {"MICU_MODEL": "legacy-fixture", "MICU_BASE_URL": "https://legacy.invalid/v1", "MICU_API_KEY": "legacy-fixture-key"}
DEEPSEEK = {"DS_MICU_MODEL": "deepseek-fixture", "DS_MICU_BASE_URL": "https://deepseek.invalid/v1", "DS_MICU_API_KEY": "ds-fixture-key"}
OPENROUTER = {
    "OPENROUTER_MODEL": "openrouter-fixture",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "OPENROUTER_API_KEY": "router-fixture-key",
}


class ModelConfigTests(TestCase):
    def test_invalid_openrouter_provider_sort_fails_before_request(self) -> None:
        with (
            patch.dict(os.environ, {**OPENROUTER, "OPENROUTER_PROVIDER_SORT": "unexpected"}, clear=True),
            self.assertRaisesRegex(ValueError, "OPENROUTER_PROVIDER_SORT"),
        ):
            build_model()

    def test_invalid_openrouter_reasoning_effort_fails_before_request(self) -> None:
        with (
            patch.dict(os.environ, {**OPENROUTER, "OPENROUTER_REASONING_EFFORT": "unexpected"}, clear=True),
            self.assertRaisesRegex(ValueError, "OPENROUTER_REASONING_EFFORT"),
        ):
            build_model()

    def test_openrouter_group_takes_priority_without_mixing_credentials(self) -> None:
        with patch.dict(os.environ, {**LEGACY, **DEEPSEEK, **OPENROUTER}, clear=True):
            model = build_model()
        self.assertEqual(model.model_name, OPENROUTER["OPENROUTER_MODEL"])
        self.assertEqual(model.openai_api_base, OPENROUTER["OPENROUTER_BASE_URL"])
        self.assertEqual(model.openai_api_key.get_secret_value(), OPENROUTER["OPENROUTER_API_KEY"])
        self.assertFalse(model.use_responses_api)
        self.assertEqual(model.extra_body, {"provider": {"require_parameters": True}})

    def test_incomplete_openrouter_group_does_not_borrow_another_key(self) -> None:
        with (
            patch.dict(os.environ, {**DEEPSEEK, **OPENROUTER, "OPENROUTER_API_KEY": ""}, clear=True),
            self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"),
        ):
            build_model()

    def test_empty_openrouter_placeholders_preserve_deepseek_selection(self) -> None:
        with patch.dict(os.environ, {**DEEPSEEK, **dict.fromkeys(OPENROUTER, " ")}, clear=True):
            model = build_model()
        self.assertEqual(model.model_name, DEEPSEEK["DS_MICU_MODEL"])

    def test_deepseek_group_is_default_when_both_are_configured(self) -> None:
        with patch.dict(os.environ, {**LEGACY, **DEEPSEEK}, clear=True):
            model = build_model()
        self.assertEqual(model.model_name, DEEPSEEK["DS_MICU_MODEL"])
        self.assertEqual(model.openai_api_base, DEEPSEEK["DS_MICU_BASE_URL"])
        self.assertEqual(model.openai_api_key.get_secret_value(), DEEPSEEK["DS_MICU_API_KEY"])

    def test_legacy_group_remains_supported(self) -> None:
        with patch.dict(os.environ, LEGACY, clear=True):
            model = build_model()
        self.assertEqual(model.model_name, LEGACY["MICU_MODEL"])
        self.assertEqual(model.openai_api_key.get_secret_value(), LEGACY["MICU_API_KEY"])

    def test_incomplete_deepseek_group_does_not_borrow_legacy_key(self) -> None:
        environment = {**LEGACY, **DEEPSEEK, "DS_MICU_API_KEY": ""}
        with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(ValueError, "DS_MICU_API_KEY"):
            build_model()

    def test_empty_deepseek_placeholders_allow_legacy_configuration(self) -> None:
        with patch.dict(os.environ, {**LEGACY, **dict.fromkeys(DEEPSEEK, " ")}, clear=True):
            model = build_model()
        self.assertEqual(model.model_name, LEGACY["MICU_MODEL"])
