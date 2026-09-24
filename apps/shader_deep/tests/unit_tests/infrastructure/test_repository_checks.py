"""仓库检查实际拒绝越界依赖和失效导航, 不对文案或文件数做快照."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.check_repository import check_imports, check_links, main


class RepositoryChecksTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Path(directory.name)
        self.source = self.app / "src" / "shader_deep"
        self.source.mkdir(parents=True)
        (self.app / "README.md").write_text("# 项目\n", encoding="utf-8")
        (self.app / "AGENTS.md").write_text("# 入口\n", encoding="utf-8")

    def source_file(self, name: str, content: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_relative_and_type_imports_cannot_bypass_domain_boundary(self) -> None:
        path = self.source_file(
            "domain/rules.py",
            "from .models import Record\nfrom ..infrastructure.storage import save\n"
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from shader_deep import agents\n",
        )
        findings = check_imports(self.app)
        self.assertEqual({finding.line for finding in findings}, {2, 5})
        self.assertTrue(all(finding.path == path for finding in findings))

    def test_runtime_dynamic_import_and_renderer_framework_are_rejected(self) -> None:
        self.source_file("runtime/runner.py", 'import importlib\nimportlib.import_module("shader_deep.agents.outline")\n')
        self.source_file("rendering/renderer.py", "import langchain_anthropic\n")
        self.source_file("domain/models.py", "from pydantic import BaseModel\n")
        self.assertEqual(len(check_imports(self.app)), 2)

    def test_active_code_cannot_reenter_legacy_but_compatibility_can_forward(self) -> None:
        self.source_file("agents/worker.py", "from shader_deep.analysis.loop import AnalysisLoop\n")
        self.source_file("analysis/loop.py", "from shader_deep.runtime.runner import AnalysisLoop\n")
        findings = check_imports(self.app)
        self.assertEqual(len(findings), 1)
        self.assertIn("shader_deep.analysis.loop", findings[0].message)

    def test_links_accept_encoded_paths_and_ignore_fenced_examples(self) -> None:
        (self.app / "设计 说明.md").write_text("# 说明\n", encoding="utf-8")
        (self.app / "README.md").write_text(
            "[说明](%E8%AE%BE%E8%AE%A1%20%E8%AF%B4%E6%98%8E.md#结构)\n"
            "[入口][guide]\n[guide]: AGENTS.md\n```md\n[示例](missing.md)\n```\n"
            "`[占位](missing.md)`\n[网站](https://example.com/missing)\n",
            encoding="utf-8",
        )
        self.assertEqual(check_links(self.app), [])

    def test_links_reject_missing_targets_and_undefined_references(self) -> None:
        (self.app / "README.md").write_text("[失效](missing.md)\n[未定义][unknown]\n", encoding="utf-8")
        findings = check_links(self.app)
        self.assertEqual({finding.line for finding in findings}, {1, 2})

    def test_source_module_readme_links_are_checked_relative_to_the_module(self) -> None:
        module = self.source_file("runtime/execution.py", '"""执行记录."""\n').parent
        readme = module / "README.md"
        readme.write_text("[有效入口](execution.py)\n[失效入口](missing.py)\n", encoding="utf-8")
        findings = check_links(self.app)
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0].path, findings[0].line), (readme, 2))

    def test_cli_fails_then_passes_after_boundary_is_repaired(self) -> None:
        path = self.source_file("runtime/runner.py", "from shader_deep.workflows.analysis import run_analysis\n")
        with patch("sys.argv", ["check_repository", "--root", str(self.app)]), redirect_stdout(StringIO()):
            self.assertEqual(main(), 1)
            path.write_text("from shader_deep.runtime.execution import AnalysisExecution\n", encoding="utf-8")
            self.assertEqual(main(), 0)
