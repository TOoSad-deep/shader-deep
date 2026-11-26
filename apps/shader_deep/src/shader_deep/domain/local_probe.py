"""整图计划之下的局部试验绑定与已验片段标识."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, model_validator

from shader_deep.domain.scene_planning import MechanismIds, RiskProbe, SketchId
from shader_deep.domain.visual_review import BoundReviewCriteria, Digest, FiniteTime, PixelRegion, Text  # noqa: TC001  # 运行时校验局部标准.

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

    from shader_deep.domain.scene_observations import SceneObservations
    from shader_deep.domain.scene_planning import EffectivePlanningSketch
    from shader_deep.domain.shader_parameters import UniformValue

ColorChannel = Annotated[float, Field(strict=True, ge=0, le=1)]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\r\n]*(?:\r\n?|\n|\Z)", re.DOTALL)


def validate_patch_fragment(fragment: str) -> None:
    """核对 GLSL 片段的边界, 防止替换影响固定基线的其余代码."""
    msg = "固定基线补丁必须是边界完整的 GLSL 片段, 不含预处理指令"
    # 未换行的行注释会吞掉替换点后面的固定代码.
    for match in _COMMENTS.finditer(fragment):
        if match.end() == len(fragment) and match[0].startswith("//") and not match[0].endswith(("\n", "\r")):
            raise ValueError(msg)
    source = _COMMENTS.sub("", fragment)
    if not source.strip() or any(token in source for token in ("#", "/*", "*/")):
        raise ValueError(msg)
    closing = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for character in source:
        if character in "([{":
            stack.append(character)
        elif character in closing and (not stack or stack.pop() != closing[character]):
            raise ValueError(msg)
    if stack:
        raise ValueError(msg)


def probe_target_refs(observations: SceneObservations, target_ids: tuple[str, ...]) -> frozenset[str]:
    """将局部目标展开为固定观察中允许回溯的 ID, 元素包括其特征."""
    keys = frozenset(target_ids)
    if not keys or not keys <= observations.identities:
        msg = "试验目标须存在于当前观察版本"
        raise ValueError(msg)
    return observations.target_refs(target_ids)


def probe_mechanism_ids(observations: SceneObservations, sketch: EffectivePlanningSketch, target_ids: tuple[str, ...]) -> tuple[str, ...]:
    """从当前生效方案派生局部目标采用的机制, 保留构造顺序."""
    refs = probe_target_refs(observations, target_ids)
    return tuple(dict.fromkeys(method for target, group in sketch.choices.items() if target in refs for method in group))


def probe_observation_dependencies(observations: SceneObservations, target_ids: tuple[str, ...]) -> dict[str, str]:
    """收集局部目标、所属元素及关系参与项的正文指纹."""
    ids = set(target_ids)
    for relation in observations.relations:
        if relation.id in ids:
            ids.update(relation.participants)
    for element in observations.elements:
        if element.id in ids:
            ids.update(element.feature_ids)
        elif ids.intersection(element.feature_ids):
            ids.add(element.id)
    return {
        item.id: hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()
        for group in (observations.elements, observations.features, observations.relations)
        for item in group
        if item.id in ids
    }


def probe_dependency_digest(
    observations: SceneObservations, sketch: EffectivePlanningSketch, target_ids: tuple[str, ...], *, reference_sha256: str, request: str
) -> str:
    """按目标比较跨规划可复用的观察、选择和机制正文, 保留每个目标内部的方法次序."""
    dependencies = probe_observation_dependencies(observations, target_ids)
    choices = {target: methods for target, methods in sketch.choices.items() if target in dependencies}
    method_ids = {method for methods in choices.values() for method in methods}
    material = {
        "reference": reference_sha256,
        "request": request,
        "observations": dependencies,
        "choices": choices,
        "mechanisms": {item.id: item.model_dump(mode="json") for item in sketch.mechanisms if item.id in method_ids},
        "conditions": sketch.conditions,
        "gaps": [gap.model_dump(mode="json") for gap in (*sketch.gaps, *sketch.observation_gaps) if not gap.refs or dependencies.keys() & gap.refs],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LocalProbeBinding(RiskProbe):
    """独立试验所需的来源、像素坐标、冻结标准与三种上下文之一."""

    planning_sha256: Digest
    dependency_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    sketch_id: SketchId
    alternative_index: Annotated[int, Field(strict=True, ge=0)] | None = None
    mechanism_ids: MechanismIds
    width: Annotated[int, Field(strict=True, gt=0)]
    height: Annotated[int, Field(strict=True, gt=0)]
    time: FiniteTime = 0.0
    target_region: PixelRegion
    criteria: BoundReviewCriteria
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=6)] = 3
    solid_rgba: tuple[ColorChannel, ColorChannel, ColorChannel, ColorChannel] | None = None
    proxy_context: Text | None = None
    baseline_glsl: Text | None = None
    patch_source: Text | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if len(set(self.mechanism_ids)) != len(self.mechanism_ids):
            msg = "局部试验机制 ID 须唯一"
            raise ValueError(msg)
        if self.target_region.right > self.width or self.target_region.bottom > self.height:
            msg = "试验区域超出固定画布"
            raise ValueError(msg)
        expected = "element" if self.target_ids[0].startswith("E") else "feature"
        task = self.criteria.request
        if task.scope != "local" or task.target_kind != expected or self.criteria.profile_id != expected or task.protocol != "visual_review_v2":
            msg = "局部试验须绑定与目标对应的 V2 局部标准"
            raise ValueError(msg)
        self._validate_mode()
        return self

    def _validate_mode(self) -> None:
        supplied = frozenset(name for name in ("solid_rgba", "proxy_context", "baseline_glsl", "patch_source") if getattr(self, name) is not None)
        expected = {
            "isolated_solid": {"solid_rgba"},
            "proxy_context": {"proxy_context"},
            "scene_baseline_patch": {"baseline_glsl", "patch_source"},
        }
        if supplied != expected[self.context_mode]:
            msg = f"{self.context_mode} 只允许其对应上下文字段: {', '.join(sorted(expected[self.context_mode]))}"
            raise ValueError(msg)
        if self.patch_source is not None and self.baseline_glsl is not None:
            start = self.baseline_glsl.find(self.patch_source)
            if start < 0 or self.baseline_glsl.find(self.patch_source, start + 1) >= 0:
                msg = "patch_source 须在固定整图基线中精确出现一次"
                raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class VerifiedSnippet:
    """由调用方从已确认局部候选制作的固定输入和评审指纹."""

    binding: LocalProbeBinding
    glsl_code: str
    uniforms: Mapping[str, UniformValue]
    preview_png: bytes
    review_criteria_sha256: str
    review_report_sha256: str

    def __post_init__(self) -> None:
        """防止片段内容为空或声称来自无关试验的固定标准."""
        if not self.glsl_code.strip() or not isinstance(self.preview_png, bytes) or not self.preview_png.startswith(_PNG_MAGIC):
            msg = "片段必须包含实际 GLSL 与 PNG 字节"
            raise ValueError(msg)
        if self.review_criteria_sha256 != self.binding.criteria.criteria_sha256 or not _SHA256.fullmatch(self.review_report_sha256):
            msg = "片段须绑定本次局部标准及有效的评审报告摘要"
            raise ValueError(msg)
        values = dict(self.uniforms)
        for name, value in values.items():
            parts = value if isinstance(value, tuple) else (value,)
            if not name or len(parts) not in {1, 2, 3, 4} or any(isinstance(part, bool) or not isfinite(part) for part in parts):
                msg = f"片段参数 {name!r} 必须为有限标量或向量"
                raise ValueError(msg)
        object.__setattr__(self, "uniforms", MappingProxyType(values))

    def metadata(self) -> dict[str, JsonValue]:
        """返回可写入整图捕获包的来源和制品身份."""
        return {
            "binding": self.binding.model_dump(mode="json", exclude_none=True, exclude={"baseline_glsl", "patch_source"}),
            "glsl_sha256": hashlib.sha256(self.glsl_code.encode("utf-8")).hexdigest(),
            "preview_sha256": hashlib.sha256(self.preview_png).hexdigest(),
            "uniforms": {name: list(value) if isinstance(value, tuple) else value for name, value in self.uniforms.items()},
            "review_criteria_sha256": self.review_criteria_sha256,
            "review_report_sha256": self.review_report_sha256,
        }
