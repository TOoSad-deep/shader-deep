"""五库完整文件包的发布和按草图取材, 不维护第二份可变业务状态."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from shader_deep.domain.five_libraries import FiveLibraries, Issue, object_index, parse_references, validate_libraries

LIBRARIES = ("elements", "features", "relations", "mechanisms", "sketches")
MARKER = re.compile(r"\[\[([^\[\]\r\n]+)\]\]")
ENTRY_SKETCHES = 6
ENTRY_GAPS = 3


class ReferenceAsset(BaseModel):
    """随包固定的参考图, 不接受包外路径."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Literal["reference.png"] = "reference.png"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportManifest(BaseModel):
    """本轮公共状态的交付投影, 字段以五库设计为准."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["five_libraries_v1"] = "five_libraries_v1"
    reference: ReferenceAsset
    target_element_id: str
    status: Literal["completed", "partial"]
    open_questions: tuple[Issue, ...] = ()
    gaps: tuple[Issue, ...] = ()


def _write_json(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _check_manifest(libraries: FiveLibraries, manifest: ReportManifest) -> None:
    validate_libraries(libraries, manifest.target_element_id)
    index = object_index(libraries)
    for issue in (*manifest.open_questions, *manifest.gaps):
        if set(issue.refs) - index.keys():
            msg = f"公共问题含包外引用: {issue.refs}"
            raise ValueError(msg)
        parse_references(issue.description, index, field="manifest.issues.description")
    if manifest.status == "completed" and manifest.gaps:
        msg = "有未完成必要工作的报告不能标记 completed"
        raise ValueError(msg)


def _entry(libraries: FiveLibraries, manifest: ReportManifest) -> str:
    target = next(item for item in libraries.elements if item.id == manifest.target_element_id)
    rows = ["# 分析报告", "", f"目标: {target.id} {target.name}; 范围: {target.region}.", f"状态: `{manifest.status}`.", "", "草图目录:", ""]
    rows.extend(f"- `{item.id}` {item.name}: {len(item.default)} 个实现目标." for item in libraries.sketches[:ENTRY_SKETCHES])
    if len(libraries.sketches) > ENTRY_SKETCHES:
        rows.append(f"- 另有 {len(libraries.sketches) - ENTRY_SKETCHES} 套草图, 完整目录见 sketches.json; 顺序沿用来源, 未排名.")
    if not libraries.sketches:
        rows.append("- 本轮没有草图产物; 查看公共缺口.")
    if manifest.gaps:
        rows.extend(["", f"未完成工作 (共 {len(manifest.gaps)} 项):", "", *(f"- {item.description}" for item in manifest.gaps[:ENTRY_GAPS])])
        if len(manifest.gaps) > ENTRY_GAPS:
            rows.append("- 其余缺口及完整未知项见 manifest.json.")
    rows.extend(
        [
            "",
            "读取: 先查看 [manifest.json](manifest.json), 再选取 [sketches.json](sketches.json) 中的草图.",
            "`read_sketch(report_dir, sketch_id)` 提供当前草图的默认取材视图; 局部备选按索引显式选择.",
            "完整定义保存在五个库文件中, 视图不替代完整包. 候选机制尚需编码与渲染验证.",
            "",
        ]
    )
    return "\n".join(rows)


def write_report_package(directory: Path, libraries: FiveLibraries, manifest: ReportManifest, reference: bytes) -> Path:
    """校验后一次发布完整文件包, 已交付目录不能原地覆盖.

    Args:
        directory: 新文件包的绝对或相对目录.
        libraries: 已选定同一版本的完整五库.
        manifest: 程序计算的本轮公共状态.
        reference: 启动时固定的 PNG 字节.

    Returns:
        已发布文件包的绝对目录.

    Raises:
        ValueError: 引用、状态或原图哈希不一致.
        FileExistsError: 文件包已经发布.
        OSError: 文件写入或发布失败.
    """
    _check_manifest(libraries, manifest)
    if hashlib.sha256(reference).hexdigest() != manifest.reference.sha256:
        msg = "文件包参考图与公共哈希不一致"
        raise ValueError(msg)
    directory = directory.resolve()
    if directory.exists():
        msg = f"已发布文件包不能覆盖: {directory}"
        raise FileExistsError(msg)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".report-staging-", dir=directory.parent))
    try:
        _write_contents(staging, libraries, manifest, reference)
        read_report_package(staging)
        staging.rename(directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return directory


def _write_contents(directory: Path, libraries: FiveLibraries, manifest: ReportManifest, reference: bytes) -> None:
    payload = libraries.model_dump(mode="json", exclude_none=True)
    for name in LIBRARIES:
        _write_json(directory / f"{name}.json", payload[name])
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json", exclude_none=True))
    (directory / "reference.png").write_bytes(reference)
    (directory / "README.md").write_text(_entry(libraries, manifest), encoding="utf-8")


def read_report_package(directory: Path) -> tuple[ReportManifest, FiveLibraries]:
    """读取并验证完整快照及固定参考图.

    !!! warning "实验性接口"
        读取 five_libraries_v1 文件包, 旧四库须显式转换.

    Args:
        directory: 包含 manifest 与五个库文件的目录.

    Returns:
        同一文件包的公共状态及完整五库.

    Raises:
        ValueError: 结构、引用、状态或原图哈希损坏.
        OSError: 文件缺失或无法读取.
    """
    manifest = ReportManifest.model_validate_json((directory / "manifest.json").read_bytes())
    payload = {name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8")) for name in LIBRARIES}
    libraries = FiveLibraries.model_validate(payload)
    _check_manifest(libraries, manifest)
    if hashlib.sha256((directory / manifest.reference.path).read_bytes()).hexdigest() != manifest.reference.sha256:
        msg = "参考图哈希校验失败"
        raise ValueError(msg)
    return manifest, libraries


def read_sketch(directory: Path, sketch_id: str, *, alternative: int | None = None) -> dict[str, JsonValue]:
    """从完整快照提供所选草图的取材视图, 不将视图冒充自包含报告.

    !!! warning "实验性接口"
        视图结构用于按需取材, 完整权威结果仍在原文件包.

    Args:
        directory: 已发布完整文件包.
        sketch_id: 显式选择的草图 ID, 不自动排名.
        alternative: 显式选择一项局部备选的零基索引; 不自动组合备选.

    Returns:
        公共状态、原草图、有效选择和相关正文; 其他引用按需回读原包.

    Raises:
        ValueError: 草图不存在或备选索引无效.
    """
    manifest, libraries = read_report_package(directory)
    sketch = next((item for item in libraries.sketches if item.id == sketch_id), None)
    if sketch is None:
        msg = f"草图不存在: {sketch_id}"
        raise ValueError(msg)
    choices, composition = dict(sketch.default), sketch.composition
    alternatives = sketch.alternatives or ()
    if alternative is not None:
        if isinstance(alternative, bool) or not isinstance(alternative, int) or not 0 <= alternative < len(alternatives):
            msg = f"无效备选索引: {alternative}"
            raise ValueError(msg)
        option = alternatives[alternative]
        choices.update(option.choices)
        composition = option.composition or composition
    selected = set(choices) | {sketch.element_id} | {identity for values in choices.values() for identity in values}
    selected.update(MARKER.findall(composition))
    _include_referenced_content(libraries, selected)
    return {
        "scope": "sketch_view",
        "manifest": manifest.model_dump(mode="json", exclude_none=True),
        "sketch": sketch.model_dump(mode="json", exclude_none=True),
        "selected": {"choices": {key: list(value) for key, value in choices.items()}, "composition": composition, "alternative": alternative},
        **_selected_content(libraries, selected),
    }


def _include_referenced_content(libraries: FiveLibraries, selected: set[str]) -> None:
    objects = {item.id: item for name in LIBRARIES for item in getattr(libraries, name)}
    participants = {item.id: {part.ref for part in item.participants} for item in libraries.relations}
    pending = list(selected)
    visited: set[str] = set()
    while pending:
        identity = pending.pop()
        if identity in visited or identity not in objects:
            continue
        visited.add(identity)
        references = set(_text_ids(objects[identity].model_dump(mode="json"))) | participants.get(identity, set())
        pending.extend(references - visited)
        selected.update(references)


def _text_ids(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return MARKER.findall(value)
    if isinstance(value, list):
        return [identity for item in value for identity in _text_ids(item)]
    if isinstance(value, dict):
        return [identity for item in value.values() for identity in _text_ids(item)]
    return []


def _selected_content(libraries: FiveLibraries, selected: set[str]) -> dict[str, JsonValue]:
    return {
        name: [item.model_dump(mode="json", exclude_none=True) for item in getattr(libraries, name) if item.id in selected]
        for name in LIBRARIES
        if name != "sketches"
    }
