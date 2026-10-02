"""捕获生成输入的文件副本和内存材料, 执行期间不跟随原报告变化."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PIL import Image

from shader_deep.domain.five_libraries import object_index, parse_references
from shader_deep.domain.generation import GenerationBinding
from shader_deep.infrastructure.storage.report_package import (
    LIBRARIES,
    _include_referenced_content,
    _selected_content,
    read_report_package,
    read_sketch,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.five_libraries import Alternative, FiveLibraries
    from shader_deep.domain.tasks import CandidateRecord
    from shader_deep.infrastructure.storage.report_package import ReportManifest

CONTENT_FILES = ("manifest.json", *(f"{name}.json" for name in LIBRARIES), "reference.png")
REPORT_FILES = (*CONTENT_FILES, "README.md")


@dataclass(frozen=True, kw_only=True)
class CapturedBaseline:
    """保留原候选 ID, 副本路径与内存正文不改写历史候选."""

    candidate_id: str
    code: str
    code_path: str
    preview_url: str | None
    preview_path: str | None


@dataclass(frozen=True, kw_only=True)
class CapturedGenerationInputs:
    """供每轮请求复用的固定材料, 不包含模型历史或运行计数."""

    binding: GenerationBinding
    reference_path: str
    reference_url: str
    width: int
    height: int
    scheme_json: str
    files: tuple[str, ...]
    baseline: CapturedBaseline | None = None


def _copy_report(source: Path, destination: Path) -> dict[str, bytes]:
    # 限定协议已知文件, 不复制目录内额外日志、配置或其他运行制品.
    contents = {name: (source / name).read_bytes() for name in REPORT_FILES}
    destination.mkdir(parents=True)
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    return contents


def _content_sha256(contents: dict[str, bytes]) -> str:
    # 固定 CONTENT_FILES 顺序; 每项为 UTF-8 文件名、NUL、8 字节大端长度、原始字节.
    # README 仅是可重建导航, 不计入 manifest、五库和原图的内容身份.
    digest = hashlib.sha256()
    for name in CONTENT_FILES:
        content = contents[name]
        digest.update(name.encode("utf-8") + b"\0" + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _png_url(content: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _reference_size(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "PNG":
            msg = "Generation reference must be a PNG image"
            raise ValueError(msg)
        image.load()
        return image.size


def _reference_content(libraries: FiveLibraries, selected: set[str]) -> dict[str, JsonValue]:
    expanded: set[str] = set()
    while True:
        _include_referenced_content(libraries, selected)
        sketches = [item for item in libraries.sketches if item.id in selected and item.id not in expanded]
        if not sketches:
            break
        # 正文可能通过特征再引用另一草图; 每个被引用草图只扩展一次, 允许引用环.
        for sketch in sketches:
            expanded.add(sketch.id)
            selected.add(sketch.element_id)
            for choices in (sketch.default, *(option.choices for option in sketch.alternatives or ())):
                selected.update(choices)
                selected.update(identity for identities in choices.values() for identity in identities)
    # 参考草图的选择仅补全其正文; 不扩展特征的全部候选, 不改变实际采用方案.
    return {
        **_selected_content(libraries, selected),
        "sketches": [item.model_dump(mode="json", exclude_none=True) for item in libraries.sketches if item.id in selected],
    }


def _issue_references(manifest: ReportManifest, libraries: FiveLibraries) -> dict[str, JsonValue]:
    index = object_index(libraries)
    selected: set[str] = set()
    for issue in (*manifest.gaps, *manifest.open_questions):
        selected.update(issue.refs)
        selected.update(parse_references(issue.description, index, field="manifest.issues.description"))
    return _reference_content(libraries, selected)


def _scheme_references(libraries: FiveLibraries, selection: JsonValue, element_id: str, option: Alternative | None) -> dict[str, JsonValue]:
    # 该对象由本包 read_sketch 构造并验证; 类型收窄不重新解释默认与备选合并规则.
    effective = cast("dict[str, JsonValue]", selection)
    choices = cast("dict[str, list[str]]", effective["choices"])
    selected = {element_id, *choices, *(identity for identities in choices.values() for identity in identities)}
    texts = [cast("str", effective["composition"])]
    if option is not None:
        texts.extend((option.reason, *(option.conditions or ())))
    index = object_index(libraries)
    for text in texts:
        selected.update(parse_references(text, index, field="generation.selected"))
    return _reference_content(libraries, selected)


def _scheme(directory: Path, sketch_id: str, alternative: int | None, manifest: ReportManifest, libraries: FiveLibraries) -> str:
    view = read_sketch(directory, sketch_id, alternative=alternative)
    sketch = next(item for item in libraries.sketches if item.id == sketch_id)
    option = sketch.alternatives[alternative] if alternative is not None and sketch.alternatives is not None else None
    payload = {
        "sketch_id": sketch.id,
        "sketch_name": sketch.name,
        "element_id": sketch.element_id,
        "manifest": view["manifest"],
        "selected": view["selected"],
        "selected_alternative": {"reason": option.reason, "conditions": option.conditions} if option is not None else None,
        "reference_content": _scheme_references(libraries, view["selected"], sketch.element_id, option),
        "issue_references": _issue_references(manifest, libraries),
        "notes": [
            "仅 selected.choices 与 selected.composition 定义采用方案; reference_content 与 issue_references 中的草图及机制均是参考, 不表示采用."
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _preview_bytes(path: str | None, root: Path) -> bytes | None:
    if path is None:
        return None
    source = root / path
    if source.suffix.lower() != ".png":
        msg = f"Expected a .png file: {source}"
        raise ValueError(msg)
    return source.read_bytes()


def _capture_baseline(baseline: CandidateRecord | None, directory: Path, root: Path) -> CapturedBaseline | None:
    if baseline is None:
        return None
    code = (root / baseline.code_path).read_bytes()
    preview = _preview_bytes(baseline.preview_path, root)
    # 先读取必需材料, 预览缺失不会被默默当作未提供; 原本没有预览仍是合法基线.
    return _write_baseline(baseline.id, code, preview, directory)


def _write_baseline(candidate_id: str, code: bytes, preview: bytes | None, directory: Path) -> CapturedBaseline:
    text = code.decode("utf-8")
    directory.mkdir(parents=True)
    code_path = directory / "baseline.glsl"
    code_path.write_bytes(code)
    preview_path = directory / "baseline.png" if preview is not None else None
    if preview_path is not None and preview is not None:
        preview_path.write_bytes(preview)
    return CapturedBaseline(
        candidate_id=candidate_id,
        code=text,
        code_path=str(code_path),
        preview_url=_png_url(preview) if preview is not None else None,
        preview_path=str(preview_path) if preview_path is not None else None,
    )


def _reuse_generation_inputs(
    source: CapturedGenerationInputs, run_dir: Path, sketch_id: str, *, alternative: int | None = None
) -> CapturedGenerationInputs:
    # 只用于模型执行前准备同批另一方案; 读取已固定的报告副本, 不重读用户原始文件.
    report_path = (run_dir / "inputs" / "report").resolve()
    _copy_report(Path(source.binding.report_path), report_path)
    manifest, libraries = read_report_package(report_path)
    scheme = _scheme(report_path, sketch_id, alternative, manifest, libraries)
    baseline = source.baseline
    captured = None
    files = [str(report_path / name) for name in REPORT_FILES]
    if baseline is not None:
        preview = base64.b64decode(baseline.preview_url.split(",", 1)[1]) if baseline.preview_url is not None else None
        captured = _write_baseline(baseline.candidate_id, baseline.code.encode("utf-8"), preview, (run_dir / "inputs" / "baseline").resolve())
        files.extend(path for path in (captured.code_path, captured.preview_path) if path is not None)
    return replace(
        source,
        binding=replace(source.binding, report_path=str(report_path), sketch_id=sketch_id, alternative=alternative),
        reference_path=str(report_path / "reference.png"),
        scheme_json=scheme,
        files=tuple(files),
        baseline=captured,
    )


def capture_generation_inputs(
    report_dir: Path,
    run_dir: Path,
    sketch_id: str,
    *,
    alternative: int | None = None,
    baseline: CandidateRecord | None = None,
    asset_root: Path | None = None,
) -> CapturedGenerationInputs:
    """固定报告和可选基线, 在副本上校验后返回可复用材料.

    !!! warning "实验性接口"
        返回工作流内部固定材料, 供单方案生成和同批方案比较复用.

    Args:
        report_dir: 调用方明确选定的完整五库报告目录.
        run_dir: 工作流已经分配的新运行目录.
        sketch_id: 本轮采用的草图标识.
        alternative: 显式采用的一项局部备选索引.
        baseline: 本轮可选的起始候选, 不改写其业务记录.
        asset_root: 相对基线文件路径的根目录, 未提供时使用当前目录.

    Returns:
        固定副本的引用、内容摘要、有效方案和实际图像字节的 data URL.

    Raises:
        ValueError: 报告结构、引用、图像或明确选择无效.
        OSError: 必需文件缺失、无法读取或副本无法写入.
    """
    report_path = (run_dir / "inputs" / "report").resolve()
    contents = _copy_report(report_dir, report_path)
    manifest, libraries = read_report_package(report_path)
    scheme = _scheme(report_path, sketch_id, alternative, manifest, libraries)
    reference = contents["reference.png"]
    width, height = _reference_size(reference)
    root = (asset_root if asset_root is not None else Path.cwd()).resolve()
    captured = _capture_baseline(baseline, (run_dir / "inputs" / "baseline").resolve(), root)
    files = [str(report_path / name) for name in REPORT_FILES]
    if captured is not None:
        files.extend(path for path in (captured.code_path, captured.preview_path) if path is not None)
    return CapturedGenerationInputs(
        binding=GenerationBinding(
            report_path=str(report_path),
            content_sha256=_content_sha256(contents),
            reference_sha256=manifest.reference.sha256,
            element_id=manifest.target_element_id,
            sketch_id=sketch_id,
            alternative=alternative,
        ),
        reference_path=str(report_path / "reference.png"),
        reference_url=_png_url(reference),
        width=width,
        height=height,
        scheme_json=scheme,
        files=tuple(files),
        baseline=captured,
    )
