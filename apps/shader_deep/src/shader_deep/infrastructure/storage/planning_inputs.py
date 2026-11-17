"""捕获规划的固定观察、用户要求及实际生成约束."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Literal

from PIL import Image
from pydantic import Field, JsonValue, field_validator

from shader_deep.domain.scene_observations import SceneObservations
from shader_deep.domain.scene_planning import PlanningModel
from shader_deep.infrastructure.storage.observation_package import ObservationManifest, _check

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.agents.generation.options import GenerationOptions

OBSERVATION_CONTENT_FILES = ("manifest.json", "elements.json", "features.json", "relations.json", "reference.png")
OBSERVATION_FILES = (*OBSERVATION_CONTENT_FILES, "README.md")


class GenerationConstraints(PlanningModel):
    """当前单 Pass 环境及本次预期渲染条件."""

    width: Annotated[int, Field(strict=True, gt=0)]
    height: Annotated[int, Field(strict=True, gt=0)]
    time: float = 0.0
    language: Literal["GLSL ES 3.00"] = "GLSL ES 3.00"
    entry_point: Literal["mainImage(out vec4, in vec2)"] = "mainImage(out vec4, in vec2)"
    passes: Literal[1] = 1
    uniforms: tuple[Literal["iResolution"], Literal["iTime"]] = ("iResolution", "iTime")
    external_textures: Literal[False] = False

    @field_validator("time", mode="before")
    @classmethod
    def _finite_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            msg = "time 必须为有限秒数"
            raise ValueError(msg)
        return value


@dataclass(frozen=True, kw_only=True)
class CapturedPlanningSource:
    """可重复装配请求的固定材料, 不保存第二套模型历史."""

    directory: Path
    observation_dir: Path
    observation_manifest: ObservationManifest
    observations: SceneObservations
    reference_path: Path
    reference_url: str
    reference_bytes: bytes
    observation_sha256: str
    request: str
    constraints: GenerationConstraints


def content_sha256(contents: dict[str, bytes]) -> str:
    """按文件名与长度边界计算固定字节集合身份.

    Args:
        contents: 本协议明确捕获的相对路径及原始字节.

    Returns:
        十六进制 SHA256.
    """
    digest = hashlib.sha256()
    for name in sorted(contents):
        content = contents[name]
        digest.update(name.encode("utf-8") + b"\0" + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def package_asset(directory: Path, name: str) -> Path:
    """定位协议白名单文件并拒绝符号链接及路径逃逸.

    Args:
        directory: 本包目录.
        name: 调用方指定的协议内相对路径.

    Returns:
        安全的本地文件路径.

    Raises:
        ValueError: 文件路径超出目录或使用符号链接.
    """
    path = directory / name
    if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
        msg = f"规划文件路径不合法: {name}"
        raise ValueError(msg)
    for parent in path.parents:
        if parent == directory:
            break
        if parent.is_symlink():
            msg = f"规划文件路径含符号链接: {name}"
            raise ValueError(msg)
    return path


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            msg = f"JSON 对象键重复: {key}"
            raise ValueError(msg)
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    msg = f"JSON 不允许 {value}"
    raise ValueError(msg)


def parse_json(content: bytes) -> JsonValue:
    """严格解析正式文件, 保留重复键与非有限值错误.

    Args:
        content: JSON 原始 UTF-8 字节.

    Returns:
        可供领域合同校验的 JSON 值.
    """
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_nonfinite)


def reference_size(content: bytes) -> tuple[int, int]:
    """从捕获字节校验 PNG 并取得原图尺寸.

    Args:
        content: 固定参考图字节.

    Returns:
        原始宽度和高度.
    """
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "PNG":
            msg = "规划参考图必须为 PNG"
            raise ValueError(msg)
        image.load()
        return image.size


def load_planning_source(directory: Path, *, observation_dir: Path | None = None) -> CapturedPlanningSource:
    """从已捕获目录重新校验并装配固定材料.

    Args:
        directory: 含 observation、request 和约束文件的材料目录.
        observation_dir: 可选的观察副本位置.

    Returns:
        以副本实际字节为依据的固定输入.
    """
    observation = observation_dir if observation_dir is not None else directory / "observation"
    contents = {f"observation/{name}": package_asset(observation, name).read_bytes() for name in OBSERVATION_FILES}
    for name in ("request.txt", "generation_constraints.json"):
        contents[name] = package_asset(directory, name).read_bytes()
    return planning_source_from_contents(directory, contents, observation_dir=observation)


def planning_source_from_contents(directory: Path, contents: dict[str, bytes], *, observation_dir: Path | None = None) -> CapturedPlanningSource:
    """用同一份已捕获字节校验并构建来源, 避免重复 IO 期间漂移.

    Args:
        directory: 当前输入或正式包目录.
        contents: 含 observation/ 前缀与要求、约束的固定字节集合.
        observation_dir: 当前观察副本的实际位置.

    Returns:
        内容与身份取自同一字节集合的来源对象.
    """
    observation = observation_dir if observation_dir is not None else directory / "observation"
    manifest = ObservationManifest.model_validate(parse_json(contents["observation/manifest.json"]))
    observations = SceneObservations.model_validate(
        {name: parse_json(contents[f"observation/{name}.json"]) for name in ("elements", "features", "relations")}
    )
    reference = contents["observation/reference.png"]
    _check(manifest, observations, reference)
    contents["observation/README.md"].decode("utf-8")
    request = contents["request.txt"].decode("utf-8")
    if not request.strip():
        msg = "规划要求必须为非空文本"
        raise ValueError(msg)
    constraints = GenerationConstraints.model_validate(parse_json(contents["generation_constraints.json"]))
    reference_size(reference)
    return CapturedPlanningSource(
        directory=directory,
        observation_dir=observation,
        observation_manifest=manifest,
        observations=observations,
        reference_path=observation / "reference.png",
        reference_bytes=reference,
        reference_url="data:image/png;base64," + base64.b64encode(reference).decode("ascii"),
        observation_sha256=content_sha256({name: contents[f"observation/{name}"] for name in OBSERVATION_CONTENT_FILES}),
        request=request,
        constraints=constraints,
    )


def capture_planning_source(
    observation_dir: Path, run_dir: Path, request: str, *, generation_options: GenerationOptions | None = None
) -> CapturedPlanningSource:
    """先复制固定字节, 再正式校验副本并绑定本轮身份.

    Args:
        observation_dir: 已发布的观察包目录.
        run_dir: 本次新运行目录.
        request: 本次整图规划要求.
        generation_options: 可选的预期生成尺寸与时间.

    Returns:
        后续请求与发布共用的固定输入.

    Raises:
        ValueError: 要求、PNG、观察或约束不合法, 或输入目录已存在.
        OSError: 捕获文件失败.
    """
    if not isinstance(request, str) or not request.strip():
        msg = "规划要求必须为非空文本"
        raise ValueError(msg)
    contents = {name: package_asset(observation_dir, name).read_bytes() for name in OBSERVATION_FILES}
    width, height = reference_size(contents["reference.png"])
    constraints = (
        GenerationConstraints(width=width, height=height)
        if generation_options is None
        else GenerationConstraints(width=generation_options.width, height=generation_options.height, time=generation_options.time)
    )
    inputs = run_dir / "inputs"
    destination = inputs / "observation"
    inputs.mkdir(parents=True)
    destination.mkdir()
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    (inputs / "request.txt").write_text(request, encoding="utf-8")
    (inputs / "generation_constraints.json").write_text(constraints.model_dump_json(indent=2) + "\n", encoding="utf-8")
    source = load_planning_source(inputs)
    (inputs / "binding.json").write_text(
        json.dumps(
            {"observation_sha256": source.observation_sha256, "source_observation_dir": str(observation_dir.resolve())}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return replace(source, directory=run_dir)
