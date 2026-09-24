"""离线检查当前知识入口的本地链接及声明的 Python 依赖边界."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path
from urllib.parse import unquote, urlsplit

PACKAGE = "shader_deep"
FRAMEWORKS = ("deepagents", "langchain", "langgraph", "openai", "anthropic")
LEGACY = ("analysis", "context", "tools", "config", "schemas", "blackboard", "artifacts", "middleware", "analysis_cli")
CURRENT = {"agents", "workflows", "domain", "runtime", "infrastructure", "imaging", "rendering"}
INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[\"'][^\n]*[\"'])?\s*\)")
REFERENCE = re.compile(r"^\s*\[([^\]]+)\]:\s*(<[^>\n]+>|\S+)")
REFERENCE_USE = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")


@dataclass(frozen=True)
class Finding:
    """可直接定位的检查失败, 不修改被检查文件."""

    path: Path
    line: int
    message: str


def _within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _forbidden(layer: str, module: str) -> bool:
    framework = any(_within(module, name) or module.startswith(name + "_") for name in FRAMEWORKS)
    application = module.startswith(PACKAGE + ".")
    if layer in {"domain", "rendering"}:
        return framework or (application and not _within(module, f"{PACKAGE}.{layer}"))
    if layer == "runtime" and any(_within(module, f"{PACKAGE}.{name}") for name in ("agents", "workflows", "compatibility")):
        return True
    return layer in CURRENT and any(_within(module, f"{PACKAGE}.{name}") for name in (*LEGACY, "compatibility"))


def _import_names(node: ast.Import | ast.ImportFrom | ast.Call, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
        return [module, *(module + "." + alias.name for alias in node.names if alias.name != "*")]
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        value = node.args[0].value
        if name in {"import_module", "__import__"} and isinstance(value, str):
            return [value] if not value.startswith(".") else []
    return []


def _check_module(path: Path, source: Path) -> list[Finding]:
    relative = path.relative_to(source)
    package = ".".join((PACKAGE, *relative.parts[:-1]))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, f"无法解析 Python: {exc.msg}")]
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            continue
        try:
            names = _import_names(node, package)
        except (ImportError, ValueError) as exc:
            findings.append(Finding(path, node.lineno, f"无法解析导入: {exc}"))
            continue
        blocked = next((name for name in names if _forbidden(relative.parts[0], name)), None)
        if blocked:
            findings.append(Finding(path, node.lineno, f"{relative.parts[0]} 不允许依赖 {blocked}; 请使用所属层的接口或移动职责"))
    return findings


def check_imports(app: Path) -> list[Finding]:
    """检查直接导入、类型导入和字面量动态导入, 不加载业务模块.

    Args:
        app: 应用目录, 包含 src/shader_deep.
    """
    source = app / "src" / PACKAGE
    if not source.is_dir():
        return [Finding(source, 1, "缺少应用源码目录")]
    return [
        finding for path in sorted(source.rglob("*.py")) if path.relative_to(source).parts[0] in CURRENT for finding in _check_module(path, source)
    ]


def _prose(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
            continue
        if not fence:
            rows.append((number, re.sub(r"(`+).*?\1", "", line)))
    return rows


def _link_failure(path: Path, line: int, raw: str) -> list[Finding]:
    target = urlsplit(raw.strip("<>"))
    if target.scheme or target.netloc or not target.path:
        return []
    local = Path(unquote(target.path))
    if local.is_absolute():
        return [Finding(path, line, f"本地链接应使用可迁移的相对路径: {raw}")]
    if not (path.parent / local).exists():
        return [Finding(path, line, f"本地链接目标不存在: {raw}")]
    return []


def _check_document(path: Path) -> list[Finding]:
    rows = _prose(path.read_text(encoding="utf-8"))
    references = {match[1].strip().casefold(): match[2] for _, row in rows if (match := REFERENCE.match(row))}
    findings: list[Finding] = []
    for line, row in rows:
        definition = REFERENCE.match(row)
        targets = [definition[2]] if definition else [match[1] for match in INLINE_LINK.finditer(row)]
        for match in REFERENCE_USE.finditer(row):
            name = (match[2] or match[1]).strip().casefold()
            if name not in references:
                findings.append(Finding(path, line, f"缺少引用链接定义: {name}"))
        findings.extend(finding for target in targets for finding in _link_failure(path, line, target))
    return findings


def check_links(app: Path) -> list[Finding]:
    """检查应用入口、docs 和源码模块 README 的行内及显式引用链接.

    Args:
        app: 应用目录; 不扫描运行制品或上游文档.
    """
    paths = [
        app / "README.md",
        app / "AGENTS.md",
        *sorted((app / "docs").rglob("*.md")),
        *sorted((app / "src" / PACKAGE).rglob("README.md")),
    ]
    return [finding for path in paths for finding in (_check_document(path) if path.is_file() else [Finding(path, 1, "缺少知识入口文件")])]


def main() -> int:
    """报告全部链接与依赖问题, 发现任一问题时返回非零状态."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    app = parser.parse_args().root.resolve()
    findings = [*check_imports(app), *check_links(app)]
    for finding in findings:
        sys.stdout.write(f"{finding.path}:{finding.line}: {finding.message}\n")
    sys.stdout.write(f"Repository checks: {'FAILED' if findings else 'OK'} ({len(findings)} findings)\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
