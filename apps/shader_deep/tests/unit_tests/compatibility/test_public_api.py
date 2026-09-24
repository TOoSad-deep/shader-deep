"""目录迁移后旧 Python 调用仍进入同一套当前实现."""

from __future__ import annotations

import importlib
from unittest import TestCase

from shader_deep import api
from shader_deep.agents.generation import SYSTEM_PROMPT
from shader_deep.agents.generation.prompts import SYSTEM_PROMPT as CANONICAL_PROMPT
from shader_deep.analysis_cli import main as old_analysis
from shader_deep.cli import main as old_generation
from shader_deep.cli.analysis import main as analysis
from shader_deep.cli.generation import main as generation


class PublicApiTests(TestCase):
    def test_previous_entrypoints_keep_function_identity_and_signatures(self) -> None:
        for module, names in (
            ("shader_deep.agents.analysis", ("run_analysis", "run_analysis_task")),
            ("shader_deep.agents.generation", ("generate_shader", "generate_task", "run_shader", "run_generation")),
            ("shader_deep.analysis.types", ("AnalysisOptions", "AnalysisOutcome")),
            ("shader_deep.config", ("GenerationOptions",)),
        ):
            old = importlib.import_module(module)
            for name in names:
                with self.subTest(module=module, name=name):
                    self.assertIs(getattr(old, name), getattr(api, name))

    def test_previous_prompt_and_cli_exports_remain_available(self) -> None:

        self.assertEqual(SYSTEM_PROMPT, CANONICAL_PROMPT)
        self.assertIs(old_analysis, analysis)
        self.assertIs(old_generation, generation)

    def test_previous_context_and_tool_packages_keep_their_exports(self) -> None:
        for old_path, current_path, names in (
            ("shader_deep.context", "shader_deep.agents.generation.context", ("GenerationContext", "build_generation_context")),
            ("shader_deep.context", "shader_deep.infrastructure.llm.messages", ("png_data_url",)),
            ("shader_deep.tools", "shader_deep.agents.generation.tools", ("RenderSession", "GenerationOutcome", "GenerationLimitError")),
        ):
            old, current = importlib.import_module(old_path), importlib.import_module(current_path)
            for name in names:
                with self.subTest(module=old_path, name=name):
                    self.assertIs(getattr(old, name), getattr(current, name))
