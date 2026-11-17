"""发布和读取全图观察包, 保持旧完整五库读取接口独立."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from shader_deep.domain.scene_observations import (
    ObservationIssue,
    ObservationModel,
    SceneObservations,
    Text,
    validate_scene_observations,
)

LIBRARIES = ("elements", "features", "relations")


class ObservationReference(ObservationModel):
    """包内固定原图与实际文件字节身份."""

    path: Literal["reference.png"] = "reference.png"
    sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


class ObservationManifest(ObservationModel):
    """观察交付状态与上下文; 不表达生成就绪或视觉验收."""

    schema_version: Literal["scene_observations_v1"] = "scene_observations_v1"
    reference: ObservationReference
    status: Literal["completed", "partial"]
    context_notes: tuple[Text, ...] = ()
    open_questions: tuple[ObservationIssue, ...] = ()
    gaps: tuple[ObservationIssue, ...] = ()


def _check(manifest: ObservationManifest, observations: SceneObservations, reference: bytes) -> None:
    validate_scene_observations(observations, context_notes=manifest.context_notes, open_questions=manifest.open_questions, gaps=manifest.gaps)
    if (manifest.status == "partial") != bool(manifest.gaps):
        msg = "manifest.status: partial 必须声明必要观察缺口, completed 不能包含 gaps"
        raise ValueError(msg)
    if hashlib.sha256(reference).hexdigest() != manifest.reference.sha256:
        msg = "reference.sha256: 固定参考图哈希校验失败"
        raise ValueError(msg)


def _asset(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.resolve().is_relative_to(directory.resolve()):
        msg = f"报告文件超出包目录: {name}"
        raise ValueError(msg)
    return candidate


def _write_json(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _line(text: str) -> str:
    return " ".join(text.split())


def _entry(manifest: ObservationManifest, observations: SceneObservations) -> str:
    rows = ["# 全图观察报告", "", f"状态: `{manifest.status}`. 观察完成不代表机制规划或视觉验收通过.", ""]
    rows.extend(f"- 上下文: {_line(note)}" for note in manifest.context_notes)
    features = {feature.id: feature for feature in observations.features}
    for element in observations.elements:
        rows.append(f"- {element.id} {_line(element.name)}; 范围: {_line(element.region)}")
        rows.extend(f"  - {identity} {_line(features[identity].description)}" for identity in element.feature_ids)
    rows.extend(f"- {relation.id} {_line(relation.description)}; 参与项: {', '.join(relation.participants)}" for relation in observations.relations)
    rows.extend(_issue_rows("未知项", manifest.open_questions))
    rows.extend(_issue_rows("必要观察缺口", manifest.gaps))
    rows.extend(
        [
            "",
            "正文: [元素](elements.json)、[特征](features.json)、[关系](relations.json).",
            "",
            "[manifest](manifest.json) 保存上下文、问题与原图身份. M/S 规划尚未执行.",
            "",
        ]
    )
    return "\n".join(rows)


def _issue_rows(label: str, issues: tuple[ObservationIssue, ...]) -> list[str]:
    return [f"- {label} ({', '.join(issue.refs) or '全图'}): {_line(issue.description)}" for issue in issues]


def _write_contents(directory: Path, observations: SceneObservations, manifest: ObservationManifest, reference: bytes) -> None:
    payload = observations.model_dump(mode="json")
    for name in LIBRARIES:
        _write_json(directory / f"{name}.json", payload[name])
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    (directory / "reference.png").write_bytes(reference)
    (directory / "README.md").write_text(_entry(manifest, observations), encoding="utf-8")


def write_observation_package(directory: Path, observations: SceneObservations, manifest: ObservationManifest, reference: bytes) -> Path:
    """写齐并正式回读校验后发布观察包, 拒绝覆盖已有目录.

    Args:
        directory: 新报告包目录.
        observations: 同一接受快照的摊平 E/F/R.
        manifest: 同一快照的上下文、问题和交付状态.
        reference: 本轮捕获的原图字节.

    Returns:
        已发布的报告目录.

    Raises:
        ValueError: 数据、哈希或状态不合法, 或目标已存在.
        OSError: 写入、校验读取或发布失败.
    """
    current = SceneObservations.model_validate(observations.model_dump(mode="json"))
    declaration = ObservationManifest.model_validate(manifest.model_dump(mode="json"))
    _check(declaration, current, reference)
    _new_destination(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        _write_contents(staging, current, declaration, reference)
        read_observation_package(staging)
        _new_destination(directory)
        staging.rename(directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return directory


def _new_destination(directory: Path) -> None:
    if directory.exists() or directory.is_symlink():
        msg = f"观察包目标已存在, 不能覆盖: {directory}"
        raise ValueError(msg)


def read_observation_package(directory: Path) -> tuple[ObservationManifest, SceneObservations]:
    """读取并校验完整 scene_observations_v1 包, 不读取旧五库协议.

    Args:
        directory: 包含 manifest、E/F/R、原图和 README 的目录.

    Returns:
        同一报告包的 manifest 与观察集合.

    Raises:
        ValueError: 协议、引用、归属、状态、路径或原图哈希损坏.
        OSError: 必需文件不存在或不可读.
    """
    manifest = ObservationManifest.model_validate_json(_asset(directory, "manifest.json").read_bytes())
    payload = {name: json.loads(_asset(directory, f"{name}.json").read_text(encoding="utf-8")) for name in LIBRARIES}
    observations = SceneObservations.model_validate(payload)
    _asset(directory, "README.md").read_text(encoding="utf-8")
    _check(manifest, observations, _asset(directory, manifest.reference.path).read_bytes())
    return manifest, observations
