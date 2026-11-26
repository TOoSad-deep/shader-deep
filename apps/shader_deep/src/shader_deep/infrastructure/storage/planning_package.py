"""发布与读取绑定固定观察的自包含 scene_planning_v1 包."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from shader_deep.domain.scene_planning import (
    EffectivePlanningSketch,
    PlanningModel,
    ScenePlanningSubmission,
    SketchId,
    build_candidate_index,
    effective_planning_sketch,
    validate_scene_planning,
)
from shader_deep.infrastructure.storage.planning_inputs import (
    OBSERVATION_CONTENT_FILES,
    OBSERVATION_FILES,
    CapturedPlanningSource,
    content_sha256,
    package_asset,
    parse_json,
    planning_source_from_contents,
)

PAYLOAD_FILES = (
    *(f"observation/{name}" for name in OBSERVATION_FILES),
    "request.txt",
    "generation_constraints.json",
    "mechanisms.json",
    "sketches.json",
    "extra_candidates.json",
    "candidates.json",
    "README.md",
)
PLANNING_FILES = ("manifest.json", *PAYLOAD_FILES)
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class PlanningManifest(PlanningModel):
    """文件与源观察身份, 以及程序派生的方案完整性."""

    schema_version: Literal["scene_planning_v1"] = "scene_planning_v1"
    content_sha256: Sha256
    observation_sha256: Sha256
    files: dict[str, Sha256]
    status: Literal["completed", "partial"]
    sketch_status: dict[SketchId, Literal["completed", "partial"]]


@dataclass(frozen=True, kw_only=True)
class PlanningPackage:
    """已经正式校验的包内容, 路径始终基于当前包目录."""

    directory: Path
    manifest: PlanningManifest
    source: CapturedPlanningSource
    report: ScenePlanningSubmission

    @property
    def content_sha256(self) -> str:
        """返回正式包稳定内容身份."""
        return self.manifest.content_sha256


def _json_bytes(value: JsonValue) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sketch_status(source: CapturedPlanningSource, report: ScenePlanningSubmission) -> dict[str, Literal["completed", "partial"]]:
    return {sketch.id: "partial" if source.observation_manifest.gaps or sketch.gaps else "completed" for sketch in report.sketches}


def _summary(source: CapturedPlanningSource, report: ScenePlanningSubmission) -> str:
    rows = [
        "# 全图机制与方案报告",
        "",
        "[观察](observation/README.md) / [机制](mechanisms.json) / [方案](sketches.json) / [组合候选](candidates.json)",
        "",
    ]
    rows.extend(("## 规划要求", "", source.request, "", "## 方案", ""))
    for sketch in report.sketches:
        rows.extend((f"- {sketch.id} {sketch.name}", f"  - 完整性为 {_sketch_status(source, report)[sketch.id]}", f"  - {sketch.composition}"))
    rows.extend(("", "## 共同继承的观察缺口", ""))
    rows.extend(f"- {', '.join(gap.refs) or '全图'} {gap.description}" for gap in source.observation_manifest.gaps)
    rows.extend(("", "具体方案缺口保存在 sketches.json。条件成立与视觉效果通过生成及人工检查确认。", ""))
    return "\n".join(rows)


def _payload(source: CapturedPlanningSource, report: ScenePlanningSubmission) -> dict[str, bytes]:
    observations = {name: package_asset(source.observation_dir, name).read_bytes() for name in OBSERVATION_FILES}
    if content_sha256({name: observations[name] for name in OBSERVATION_CONTENT_FILES}) != source.observation_sha256:
        msg = "固定观察文件已在规划运行期间改变"
        raise ValueError(msg)
    payload = {f"observation/{name}": content for name, content in observations.items()}
    payload["request.txt"] = source.request.encode("utf-8")
    payload["generation_constraints.json"] = _json_bytes(source.constraints.model_dump(mode="json"))
    values = report.model_dump(mode="json", exclude_none=True)
    for name in ("mechanisms", "sketches", "extra_candidates"):
        payload[f"{name}.json"] = _json_bytes(values[name])
    payload["candidates.json"] = _json_bytes([row.model_dump(mode="json", exclude_none=True) for row in build_candidate_index(report)])
    payload["README.md"] = _summary(source, report).encode("utf-8")
    return payload


def _manifest(source: CapturedPlanningSource, report: ScenePlanningSubmission, contents: dict[str, bytes]) -> PlanningManifest:
    statuses = _sketch_status(source, report)
    return PlanningManifest(
        content_sha256=content_sha256(contents),
        observation_sha256=source.observation_sha256,
        files={name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
        status="completed" if "completed" in statuses.values() else "partial",
        sketch_status=statuses,
    )


def _new_destination(directory: Path) -> None:
    if directory.exists() or directory.is_symlink():
        msg = f"规划包目标已存在, 不能覆盖: {directory}"
        raise ValueError(msg)


def write_planning_package(directory: Path, source: CapturedPlanningSource, report: ScenePlanningSubmission) -> Path:
    """写齐并正式回读校验后发布全图规划包.

    Args:
        directory: 全新正式报告目录.
        source: 规划运行中固定的来源材料.
        report: 接受时冻结的 M/S 正文.

    Returns:
        可独立迁移和正式读取的报告目录.

    Raises:
        ValueError: 领域、来源、内容身份不合法或目标存在.
        OSError: 写入、回读或发布失败.
    """
    current = ScenePlanningSubmission.model_validate(report.model_dump(mode="json", exclude_none=True))
    validate_scene_planning(current, source.observations)
    contents = _payload(source, current)
    manifest = _manifest(source, current, contents)
    _new_destination(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        _write_contents(staging, contents, manifest)
        read_planning_package(staging)
        _new_destination(directory)
        staging.rename(directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return directory


def _write_contents(directory: Path, contents: dict[str, bytes], manifest: PlanningManifest) -> None:
    for name, content in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (directory / "manifest.json").write_bytes(_json_bytes(manifest.model_dump(mode="json")))


def _verified_contents(directory: Path, manifest: PlanningManifest) -> dict[str, bytes]:
    if set(manifest.files) != set(PAYLOAD_FILES):
        msg = "规划 manifest.files 与本协议必需文件不一致"
        raise ValueError(msg)
    contents = {name: package_asset(directory, name).read_bytes() for name in PAYLOAD_FILES}
    for name, content in contents.items():
        if hashlib.sha256(content).hexdigest() != manifest.files[name]:
            msg = f"规划文件哈希校验失败: {name}"
            raise ValueError(msg)
    if content_sha256(contents) != manifest.content_sha256:
        msg = "规划包 content_sha256 校验失败"
        raise ValueError(msg)
    return contents


def _check_derived(manifest: PlanningManifest, source: CapturedPlanningSource, report: ScenePlanningSubmission, candidates: JsonValue) -> None:
    if source.observation_sha256 != manifest.observation_sha256:
        msg = "规划绑定的观察内容身份不一致"
        raise ValueError(msg)
    if _sketch_status(source, report) != manifest.sketch_status:
        msg = "规划逐方案完整性与实际缺口不一致"
        raise ValueError(msg)
    status = "completed" if "completed" in manifest.sketch_status.values() else "partial"
    if status != manifest.status:
        msg = "规划汇总状态与方案完整性不一致"
        raise ValueError(msg)
    expected = [row.model_dump(mode="json", exclude_none=True) for row in build_candidate_index(report)]
    if candidates != expected:
        msg = "规划候选索引与默认及备选生效视图不一致"
        raise ValueError(msg)


def read_planning_package(directory: Path) -> PlanningPackage:
    """读取固定文件并重新校验新协议和全部生效方案.

    Args:
        directory: scene_planning_v1 自包含报告目录.

    Returns:
        以当前包路径重建的来源与规划正文.

    Raises:
        ValueError: 协议、哈希、路径、派生结果或领域规则损坏.
        OSError: 必需文件缺失或不可读.
    """
    directory = Path(directory)
    manifest = PlanningManifest.model_validate(parse_json(package_asset(directory, "manifest.json").read_bytes()))
    contents = _verified_contents(directory, manifest)
    report = ScenePlanningSubmission.model_validate(
        {name: parse_json(contents[f"{name}.json"]) for name in ("mechanisms", "sketches", "extra_candidates")}
    )
    source = planning_source_from_contents(directory, contents)
    validate_scene_planning(report, source.observations, require_probe_mechanisms=False)
    _check_derived(manifest, source, report, parse_json(contents["candidates.json"]))
    return PlanningPackage(directory=directory, manifest=manifest, source=source, report=report)


def read_planning_sketch(directory: Path, sketch_id: str, *, alternative_index: int | None = None) -> EffectivePlanningSketch:
    """从正式包显式读取一套整图生效方案.

    Args:
        directory: 全图规划报告目录.
        sketch_id: 本报告的 S 身份.
        alternative_index: 可选的一项零基局部备选.

    Returns:
        选中机制、组合、条件和两类缺口.
    """
    package = read_planning_package(directory)
    return effective_planning_sketch(
        package.report,
        package.source.observations,
        sketch_id,
        alternative_index=alternative_index,
        upstream_gaps=package.source.observation_manifest.gaps,
    )
