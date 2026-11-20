"""捕获视觉评审的固定双图, 发布和读取自包含报告包."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tempfile
import warnings
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import Field, JsonValue

from shader_deep.domain.review_actions import ReviewActionSummary, summarize_review_actions
from shader_deep.domain.review_coverage import validate_coverage
from shader_deep.domain.review_items import CheckAnswer, summarize_checks, validate_check_revisions
from shader_deep.domain.visual_review import (
    BoundReviewCriteria,
    PixelRegion,
    ReviewEvidence,
    ReviewModel,
    ReviewRequest,
    ReviewSubmission,
    ReviewVerdict,
    bind_review_criteria,
    derive_verdict,
    review_profile,
    validate_bound_criteria,
    validate_review_submission,
)
from shader_deep.imaging.png16 import BIT_DEPTH, crop_png16, decode_png16
from shader_deep.imaging.png_chunks import png_chunks
from shader_deep.imaging.review_evidence import measure_review_evidence
from shader_deep.infrastructure.storage.planning_inputs import content_sha256, parse_json

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import CoverageEvidence

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_777_216
MAX_TEXT_BYTES = 8 * 1024 * 1024
INPUT_FILES = frozenset({"reference.png", "rendered.png", "task.json", "criteria.json", "rules/SKILL.md", "rules/profile.md"})
VIEW_FILES = frozenset({"views/reference.png", "views/rendered.png"})
# 裁剪不改变颜色编码, 保留 PNG 中影响显示解释的颜色和 HDR 元数据原块.
COLOR_CHUNKS = frozenset({b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"cICP", b"sBIT", b"cLLI", b"mDCV"})
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ReviewManifest(ReviewModel):
    """正式包的固定文件集合及内容身份."""

    schema_version: Literal["visual_review_v1", "visual_review_v2"] = "visual_review_v1"
    input_sha256: Digest
    content_sha256: Digest
    files: dict[str, Digest]


class ReviewReport(ReviewModel):
    """含固定标准和派生结论的唯一正式报告正文."""

    review_id: str
    criteria: BoundReviewCriteria
    input_sha256: Digest
    submission: ReviewSubmission
    verdict: ReviewVerdict
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: ReviewEvidence | None = Field(default=None, exclude_if=lambda value: value is None)
    check_answers: tuple[CheckAnswer, ...] | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def action_summary(self) -> ReviewActionSummary:
        """按已核验条目派生修订及补证摘要, 保持归档报告的字节兼容."""
        return summarize_review_actions(self.submission)


@dataclass(frozen=True, kw_only=True)
class CapturedReviewSource:
    """请求与发布共用固定字节, 原文件改变不影响本轮材料."""

    directory: Path
    criteria: BoundReviewCriteria
    contents: tuple[tuple[str, bytes], ...]
    rule_text: str
    profile_text: str
    input_sha256: str
    images: tuple[tuple[str, str], ...]
    reference_size: tuple[int, int]
    rendered_size: tuple[int, int]
    evidence: ReviewEvidence | None = None

    @property
    def coverage(self) -> CoverageEvidence | None:
        """返回固定视图的覆盖证据."""
        return self.evidence.coverage if self.evidence is not None else None

    @property
    def request(self) -> ReviewRequest:
        """返回与本轮标准绑定的任务."""
        return self.criteria.request

    @property
    def reference_url(self) -> str:
        """返回固定完整参考图的实际图像载荷."""
        return self.images[0][1]

    @property
    def rendered_url(self) -> str:
        """返回固定完整候选图的实际图像载荷."""
        return self.images[1][1]


@dataclass(frozen=True, kw_only=True)
class ReviewPackage:
    """已经正式核验的完整报告与固定输入."""

    directory: Path
    manifest: ReviewManifest
    source: CapturedReviewSource
    report: ReviewReport

    @property
    def content_sha256(self) -> str:
        """返回正式包的稳定内容身份."""
        return self.manifest.content_sha256


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _url(content: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _png_size(content: bytes) -> tuple[int, int]:
    if len(content) > MAX_IMAGE_BYTES:
        msg = "评审 PNG 超过文件容量限制"
        raise ValueError(msg)
    for _ in png_chunks(content):
        pass  # 在入模前验证包括 IDAT 和 IEND 在内的全部块.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format != "PNG" or getattr(image, "is_animated", False):
                    msg = "评审只接受静态 PNG"
                    raise ValueError(msg)
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    msg = "评审 PNG 超过像素容量限制"
                    raise ValueError(msg)
                image.load()
                return image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        msg = f"评审 PNG 无法解码: {error}"
        raise ValueError(msg) from error


def _crop(content: bytes, region: PixelRegion, size: tuple[int, int]) -> bytes:
    width, height = size
    if region.right > width or region.bottom > height:
        msg = "评审区域超出对应原图边界"
        raise ValueError(msg)
    if content[24] == BIT_DEPTH:
        return _preserve_color_chunks(content, crop_png16(content, (region.left, region.top, region.right, region.bottom)))
    output = io.BytesIO()
    with Image.open(io.BytesIO(content)) as image, image.crop((region.left, region.top, region.right, region.bottom)) as cropped:
        if _low_depth_gray(content):
            with _expand_gray(cropped, content) as expanded:
                expanded.save(output, format="PNG")
        else:
            cropped.save(output, format="PNG")
    return _preserve_color_chunks(content, output.getvalue())


def _low_depth_gray(content: bytes) -> bool:
    return content[25] == 0 and content[24] in (1, 2, 4)


def _expand_gray(image: Image.Image, content: bytes) -> Image.Image:
    # Pillow 将低位深样本扩展到 0..255, 透明键需从原始 tRNS 以相同比例扩展.
    gray = image.convert("L")
    for chunk in png_chunks(content):
        if chunk[4:8] == b"tRNS":
            key = int.from_bytes(chunk[8:-4], "big")
            gray.info["transparency"] = key * 255 // ((1 << content[24]) - 1)
            break
    return gray


def _rgba_pixels(content: bytes) -> bytes:
    with Image.open(io.BytesIO(content)) as image:
        if _low_depth_gray(content):
            with _expand_gray(image, content) as gray:
                return gray.convert("RGBA").tobytes()
        return image.convert("RGBA").tobytes()


def _color_chunks(content: bytes) -> tuple[bytes, ...]:
    return tuple(chunk for chunk in png_chunks(content) if chunk[4:8] in COLOR_CHUNKS)


def _preserve_color_chunks(original: bytes, cropped: bytes) -> bytes:
    # Pillow 默认保存只继承部分元数据. 原样复制颜色块及 CRC, 避免经浮点重编码改变其含义.
    chunks = tuple(png_chunks(cropped))
    return cropped[:8] + chunks[0] + b"".join(_color_chunks(original)) + b"".join(chunk for chunk in chunks[1:] if chunk[4:8] not in COLOR_CHUNKS)


def _read_file(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        msg = f"评审文件超过容量限制: {path.name}"
        raise ValueError(msg)
    content = path.read_bytes()
    if len(content) > limit:
        msg = f"评审文件超过容量限制: {path.name}"
        raise ValueError(msg)
    return content


def _asset(directory: Path, name: str) -> Path:
    path = directory / name
    if directory.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
        msg = f"评审包文件路径不合法: {name}"
        raise ValueError(msg)
    for parent in path.parents:
        if parent == directory:
            break
        if parent.is_symlink():
            msg = f"评审包文件路径含符号链接: {name}"
            raise ValueError(msg)
    return path


def _expected_files(request: ReviewRequest) -> frozenset[str]:
    views = set()
    if request.reference_region is not None:
        views.add("views/reference.png")
    if request.rendered_region is not None:
        views.add("views/rendered.png")
    return INPUT_FILES | views


def _verify_view(expected: bytes, actual: bytes, role: str, region: PixelRegion) -> None:
    if _png_size(actual) != (region.right - region.left, region.bottom - region.top):
        msg = f"评审局部视图尺寸与固定区域不一致: {role}"
        raise ValueError(msg)
    if _color_chunks(expected) != _color_chunks(actual):
        msg = f"评审局部视图的颜色元数据与固定原图不一致: {role}"
        raise ValueError(msg)
    if expected[24] == BIT_DEPTH or actual[24] == BIT_DEPTH:
        if decode_png16(expected) != decode_png16(actual):
            msg = f"评审局部视图的 16 位样本或透明语义与固定原图不一致: {role}"
            raise ValueError(msg)
    elif _rgba_pixels(expected) != _rgba_pixels(actual):
        msg = f"评审局部视图不等于固定原图的指定区域: {role}"
        raise ValueError(msg)


def _prepare_image(contents: dict[str, bytes], role: str, region: PixelRegion | None, *, capturing: bool) -> tuple[int, int]:
    original = contents[f"{role}.png"]
    size = _png_size(original)
    if region is not None:
        view = f"views/{role}.png"
        expected = _crop(original, region, size)
        if capturing:
            _png_size(expected)
            contents[view] = expected
        else:
            _verify_view(expected, contents[view], role, region)
    return size


def _images(contents: dict[str, bytes], request: ReviewRequest) -> tuple[tuple[str, str], ...]:
    images = [
        ("参考整图 reference (局部任务用于定位与上下文)", _url(contents["reference.png"])),
        ("候选整图 rendered (实际渲染结果)", _url(contents["rendered.png"])),
    ]
    if request.coverage is not None:
        images[0] = (f"参考输入 reference 采集窗口={request.coverage.reference_window.model_dump_json()}", images[0][1])
        images[1] = (f"候选输入 rendered 采集窗口={request.coverage.rendered_window.model_dump_json()}", images[1][1])
    for role, region in (("reference", request.reference_region), ("rendered", request.rendered_region)):
        if region is not None:
            label = "参考局部" if role == "reference" else "候选局部"
            images.append((f"{label} {role} 区域={region.model_dump_json()}", _url(contents[f"views/{role}.png"])))
    return tuple(images)


def _measurement_input(content: bytes, size: tuple[int, int]) -> bytes:
    if content[24] == BIT_DEPTH:
        msg = "region_stats 以 8 位编码 RGB 为单位, 16 位 PNG 暂不支持区域测量"
        raise ValueError(msg)
    if _low_depth_gray(content):
        # 与局部视图使用同样的灰度扩展和透明键换算, 保持 PNG 坐标不变.
        return _crop(content, PixelRegion(left=0, top=0, right=size[0], bottom=size[1]), size)
    return content


def _validate_rule_protocol(request: ReviewRequest, rule_text: str) -> None:
    # 历史 V1 规则没有协议标记. V2 保留固定的头部标记, 规则正文可随包独立归档.
    declared = "visual_review_v1"
    lines = iter(rule_text.splitlines())
    if next(lines, "") == "---":
        for line in lines:
            if line == "---":
                break
            key, _, value = line.partition(":")
            if key == "review_protocol":
                declared = value.strip()
    if declared != request.protocol:
        msg = "评审共同规则的协议与固定任务不一致"
        raise ValueError(msg)


def _source(directory: Path, contents: dict[str, bytes], *, capturing: bool = False) -> CapturedReviewSource:
    request = ReviewRequest.model_validate(parse_json(contents["task.json"]))
    criteria = BoundReviewCriteria.model_validate(parse_json(contents["criteria.json"]))
    rule_text, profile_text = (contents[name].decode("utf-8") for name in ("rules/SKILL.md", "rules/profile.md"))
    _validate_rule_protocol(request, rule_text)
    validate_bound_criteria(criteria, rule_text, profile_text)
    if request != criteria.request:
        msg = "评审任务与冻结标准不一致"
        raise ValueError(msg)
    if set(contents) != (INPUT_FILES if capturing else _expected_files(request)):
        msg = "评审输入文件与固定任务的区域声明不一致"
        raise ValueError(msg)
    reference_size = _prepare_image(contents, "reference", request.reference_region, capturing=capturing)
    rendered_size = _prepare_image(contents, "rendered", request.rendered_region, capturing=capturing)
    images = (contents["reference.png"], contents["rendered.png"])
    sizes = (reference_size, rendered_size)
    if request.samples:
        images = (_measurement_input(images[0], reference_size), _measurement_input(images[1], rendered_size))
    return CapturedReviewSource(
        directory=directory,
        criteria=criteria,
        contents=tuple(sorted(contents.items())),
        rule_text=rule_text,
        profile_text=profile_text,
        input_sha256=content_sha256(contents),
        images=_images(contents, request),
        reference_size=reference_size,
        rendered_size=rendered_size,
        evidence=measure_review_evidence(request, images, sizes, directory),
    )


def _write_contents(directory: Path, contents: dict[str, bytes]) -> None:
    for name, content in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def read_captured_review_source(directory: Path) -> CapturedReviewSource:
    """从运行目录读取捕获副本, 复核区域、模板和程序证据.

    Args:
        directory: 原评审运行目录.

    Returns:
        绑定原运行身份的固定评审材料.
    """
    inputs = _asset(directory, "inputs")
    task = _read_file(_asset(inputs, "task.json"), MAX_TEXT_BYTES)
    request = ReviewRequest.model_validate(parse_json(task))
    contents = {"task.json": task}
    for name in _expected_files(request) - contents.keys():
        limit = MAX_IMAGE_BYTES if name.endswith(".png") else MAX_TEXT_BYTES
        contents[name] = _read_file(_asset(inputs, name), limit)
    return _source(directory, contents)


def capture_review_source(reference_path: Path, rendered_path: Path, directory: Path, request: ReviewRequest) -> CapturedReviewSource:
    """捕获指定双图、任务与规则, 后续请求只使用固定字节.

    Args:
        reference_path: 参考 PNG 文件.
        rendered_path: 指定候选的实际渲染 PNG 文件.
        directory: 新运行目录.
        request: 调用方已经确定范围和检查内容的任务.

    Returns:
        含完整图与可选局部视图的固定来源.

    Raises:
        ValueError: 图像、区域或任务无效, 或捕获目标已经存在.
        OSError: 读取或保存输入失败.
    """
    resource = files("shader_deep.resources").joinpath("skills", "visual-review")
    rule_name = "items.md" if request.protocol == "visual_review_v2" else "SKILL.md"
    rule_text = resource.joinpath(rule_name).read_text(encoding="utf-8")
    profile_text = resource.joinpath("profiles", f"{review_profile(request)}.md").read_text(encoding="utf-8")
    criteria = bind_review_criteria(request, rule_text, profile_text)
    contents = _capture_contents(reference_path, rendered_path, criteria, rule_text, profile_text)
    source = _source(directory, contents, capturing=True)
    _new_destination(directory / "inputs")
    _write_contents(directory / "inputs", contents)
    return source


def _capture_contents(reference: Path, rendered: Path, criteria: BoundReviewCriteria, rule: str, profile: str) -> dict[str, bytes]:
    try:
        images = {"reference.png": _read_file(reference, MAX_IMAGE_BYTES), "rendered.png": _read_file(rendered, MAX_IMAGE_BYTES)}
    except OSError as error:
        msg = f"无法读取评审输入 PNG: {error}"
        raise ValueError(msg) from error
    return {
        **images,
        "task.json": _json_bytes(criteria.request.model_dump(mode="json")),
        "criteria.json": _json_bytes(criteria.model_dump(mode="json")),
        "rules/SKILL.md": rule.encode("utf-8"),
        "rules/profile.md": profile.encode("utf-8"),
    }


def _new_destination(directory: Path) -> None:
    if directory.exists() or directory.is_symlink():
        msg = f"评审目标已存在, 不能覆盖: {directory}"
        raise ValueError(msg)


def _validate_submission(source: CapturedReviewSource, submission: ReviewSubmission, answers: tuple[CheckAnswer, ...] | None) -> None:
    if source.request.protocol == "visual_review_v1":
        if answers is not None:
            msg = "V1 评审不能携带逐项答案"
            raise ValueError(msg)
        validate_review_submission(submission, source.criteria)
        validate_coverage(submission, source.criteria, source.coverage)
        return
    if answers is None:
        msg = "V2 评审缺少逐项答案清单"
        raise ValueError(msg)
    expected = summarize_checks(source.criteria, source.coverage, answers, summary=submission.summary, limitations=submission.limitations)
    if submission != expected:
        msg = "逐项答案及程序覆盖证据与报告汇总不一致"
        raise ValueError(msg)


def write_review_package(
    directory: Path,
    source: CapturedReviewSource,
    submission: ReviewSubmission,
    *,
    metadata: dict[str, JsonValue] | None = None,
    check_answers: tuple[CheckAnswer, ...] | None = None,
) -> Path:
    """写齐固定输入与报告, 正式回读后发布新目录.

    Args:
        directory: 尚不存在的正式包路径.
        source: 本轮实际采用的固定材料.
        submission: 领域校验通过的模型提交.
        metadata: 程序提取的模型、预算和执行记录.
        check_answers: V2 采用的逐项答案, 全部必查项缺视图时为空元组.

    Returns:
        可移动后正式回读的报告目录.

    Raises:
        ValueError: 合同、来源或目标无效.
        OSError: 写入、回读或发布失败.
    """
    _validate_submission(source, submission, check_answers)
    validate_check_revisions(source.criteria, source.coverage, check_answers, metadata or {})
    contents = dict(source.contents)
    # 捕获字节已固定. 发布前检查身份, 暂存包的正式读取器独立复核像素与标准.
    if content_sha256(contents) != source.input_sha256:
        msg = "评审固定材料身份不一致"
        raise ValueError(msg)
    report = ReviewReport(
        review_id=source.directory.name,
        criteria=source.criteria,
        input_sha256=source.input_sha256,
        submission=submission,
        verdict=derive_verdict(submission),
        metadata=metadata or {},
        evidence=source.evidence,
        check_answers=check_answers,
    )
    contents["review.json"] = _json_bytes(report.model_dump(mode="json"))
    manifest = ReviewManifest(
        schema_version=source.request.protocol,
        input_sha256=source.input_sha256,
        content_sha256=content_sha256(contents),
        files={name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    )
    return _publish(directory, contents, manifest)


def _publish(directory: Path, contents: dict[str, bytes], manifest: ReviewManifest) -> Path:
    _new_destination(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        _write_contents(staging, {**contents, "manifest.json": _json_bytes(manifest.model_dump(mode="json"))})
        read_review_package(staging)
        _new_destination(directory)
        staging.rename(directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return directory


def _verified_contents(directory: Path, manifest: ReviewManifest) -> dict[str, bytes]:
    names = set(manifest.files)
    if not INPUT_FILES | {"review.json"} <= names or not names <= INPUT_FILES | VIEW_FILES | {"review.json"}:
        msg = "评审 manifest.files 与协议文件集合不一致"
        raise ValueError(msg)
    contents = {name: _read_file(_asset(directory, name), MAX_IMAGE_BYTES if name.endswith(".png") else MAX_TEXT_BYTES) for name in names}
    if any(hashlib.sha256(content).hexdigest() != manifest.files[name] for name, content in contents.items()):
        msg = "评审文件哈希校验失败"
        raise ValueError(msg)
    if content_sha256(contents) != manifest.content_sha256:
        msg = "评审包内容身份不一致"
        raise ValueError(msg)
    return contents


def read_review_package(directory: Path) -> ReviewPackage:
    """正式回读输入、规则、提交及派生结论.

    Args:
        directory: 自包含的 V1 或 V2 评审报告目录.

    Returns:
        以当前目录和实际文件字节装配的已校验报告.

    Raises:
        ValueError: 包内路径、内容身份、图像或领域合同无效.
        OSError: 正式文件无法读取.
    """
    manifest = ReviewManifest.model_validate(parse_json(_read_file(_asset(directory, "manifest.json"), MAX_TEXT_BYTES)))
    contents = _verified_contents(directory, manifest)
    report = ReviewReport.model_validate(parse_json(contents.pop("review.json")))
    source = _source(directory, contents)
    if manifest.schema_version != source.request.protocol:
        msg = "评审包协议与固定任务不一致"
        raise ValueError(msg)
    if report.check_answers is None and "check_answers" in report.model_fields_set:
        msg = "逐项答案必须为数组, V1 省略此字段"
        raise ValueError(msg)
    _validate_submission(source, report.submission, report.check_answers)
    validate_check_revisions(source.criteria, source.coverage, report.check_answers, report.metadata)
    if report.evidence != source.evidence:
        msg = "评审报告证据与固定双图及任务不一致"
        raise ValueError(msg)
    if report.criteria != source.criteria or report.input_sha256 != source.input_sha256 or manifest.input_sha256 != source.input_sha256:
        msg = "评审报告与固定输入身份不一致"
        raise ValueError(msg)
    if report.verdict != derive_verdict(report.submission):
        msg = "评审报告结论与问题及未评项不一致"
        raise ValueError(msg)
    return ReviewPackage(directory=directory, manifest=manifest, source=source, report=report)
