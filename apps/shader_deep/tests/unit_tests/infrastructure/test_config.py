"""确保模型、服务地址和凭据来自同一配置组."""

from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from shader_deep.infrastructure.llm.client import build_model

LEGACY = {"MICU_MODEL": "legacy-fixture", "MICU_BASE_URL": "https://legacy.invalid/v1", "MICU_API_KEY": "legacy-fixture-key"}
DEEPSEEK = {"DS_MICU_MODEL": "deepseek-fixture", "DS_MICU_BASE_URL": "https://deepseek.invalid/v1", "DS_MICU_API_KEY": "ds-fixture-key"}


class ModelConfigTests(TestCase):
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
