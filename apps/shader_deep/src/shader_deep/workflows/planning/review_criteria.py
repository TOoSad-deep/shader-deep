"""只从参考图整理整图草案, 按冻结来源派生局部评审标准."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Annotated, Self

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from pydantic import ConfigDict, Field, ValidationError, model_validator

from shader_deep.domain.local_probe import LocalProbeBinding, probe_dependency_digest, probe_mechanism_ids, probe_target_refs, validate_patch_fragment
from shader_deep.domain.scene_planning import effective_planning_sketch
from shader_deep.domain.visual_review import (
    DIMENSION_IDS,
    CheckSource,
    DimensionCriteria,
    PixelRegion,
    RequiredRegion,
    ReviewCoverage,
    ReviewModel,
    ReviewRequest,
    Text,
)
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.storage.observation_package import read_observation_package
from shader_deep.infrastructure.storage.planning_inputs import package_asset, reference_size

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import HumanMessage

    from shader_deep.domain.scene_observations import SceneObservations
    from shader_deep.domain.scene_planning import RiskProbe
    from shader_deep.domain.visual_review import BoundReviewCriteria
    from shader_deep.infrastructure.storage.planning_package import PlanningPackage

ORGANIZER_PROMPT = (
    "只看本次参考图、E/F/R、上下文备注及用户要求, 为人整理可复核的整图标准。"
    "必须填写工具的六个顶层字段 content、layout、shape、color、surface、relations。"
    "每个字段只选一种形式。有适用要求时写一个带 checks 的对象。每条检查直接写清楚图上什么部位应呈现什么样子, 并附同一行的 source_ref。"
    "例如输入中的 F7 若描述圆形且能看见轮廓, 对应字段可填 "
    '"shape":{"checks":[{"check":"圆的外轮廓连续闭合","source_ref":"F7","region":null,"tolerance":null}]}。'
    "实际提交时使用这张图的事实和输入已有的 E/F/R ID。直接来自用户的要求可用 user_requirement。"
    "region 为原图左上原点、右下不含的像素矩形, 无法定位填 null。tolerance 仅取用户已给定的容差, 其余填 null。"
    "某维没有适用检查时只写具体 reason, 例如仅有单个物体时可填 "
    '"relations":{"reason":"画面只有一个独立物体, 没有遮挡或相邻关系可比"}。'
    "每个字段都需写完整的可核查要求或具体排除理由。"
)


class SceneCriteriaDraft(ReviewModel):
    """编译后的六维草案, 也可回读旧版逐维输出."""

    dimensions: Annotated[tuple[DimensionCriteria, ...], Field(min_length=6, max_length=6)]
    required_regions: tuple[RequiredRegion, ...] = Field(default=(), description="顶层数组, 汇总六维检查的参考图矩形。")

    @model_validator(mode="after")
    def _region_bindings(self) -> Self:
        if any(len(row.source_refs) != len(row.required_checks) for row in self.dimensions):
            msg = "每个必查项须提供一个来源"
            raise ValueError(msg)
        checks = {(row.id, check) for row in self.dimensions for check in row.required_checks}
        seen = set()
        for item in self.required_regions:
            key = item.dimension, item.check
            if key not in checks or key in seen:
                msg = "每个区域必须唯一绑定到一个已有必查项"
                raise ValueError(msg)
            seen.add(key)
        return self


class _SceneCheck(ReviewModel):
    """一条参考图可直接核查、可追溯的要求."""

    check: Text
    source_ref: CheckSource
    region: PixelRegion | None
    tolerance: Text | None


class _Applicable(ReviewModel):
    """该维度有可从参考图复核的检查."""

    checks: Annotated[tuple[_SceneCheck, ...], Field(min_length=1)]


class _Excluded(ReviewModel):
    """该维度不适用, 须给出具体原因."""

    reason: Text


class _SceneCriteriaSlots(ReviewModel):
    """填写全部六个视觉维度, 每个维度提交 checks 或 reason."""

    model_config = ConfigDict(title="SceneCriteriaDraft")

    content: _Applicable | _Excluded
    layout: _Applicable | _Excluded
    shape: _Applicable | _Excluded
    color: _Applicable | _Excluded
    surface: _Applicable | _Excluded
    relations: _Applicable | _Excluded

    def compile(self) -> SceneCriteriaDraft:
        """用所属槽绑定每条检查的维度、来源和矩形, 保留原六维冻结合同."""
        dimensions = []
        regions = []
        for identity in DIMENSION_IDS:
            slot: _Applicable | _Excluded = getattr(self, identity)
            if isinstance(slot, _Excluded):
                dimensions.append(DimensionCriteria(id=identity, excluded_checks=(slot.reason,)))
                continue
            dimensions.append(
                DimensionCriteria(
                    id=identity,
                    required_checks=tuple(row.check for row in slot.checks),
                    source_refs=tuple(row.source_ref for row in slot.checks),
                    tolerances=tuple(dict.fromkeys(row.tolerance for row in slot.checks if row.tolerance is not None)),
                )
            )
            regions.extend(RequiredRegion(dimension=identity, check=row.check, region=row.region) for row in slot.checks if row.region is not None)
        return SceneCriteriaDraft(dimensions=tuple(dimensions), required_regions=tuple(regions))


class InvalidCriteriaOutputError(ValueError):
    """模型返回的标准无法通过草案结构或业务校验."""


def draft_scene_review_criteria(
    observation_dir: Path,
    request: str,
    *,
    model: BaseChatModel | None = None,
    previous: ReviewRequest | None = None,
    affected_sources: tuple[str, ...] = (),
    redraft_reason: str | None = None,
) -> ReviewRequest:
    """用一次模型调用生成供门禁 A 审阅和编辑的整图 V2 标准草案.

    Args:
        observation_dir: 已发布的固定观察包.
        request: 本轮用户要求.
        model: 可注入模型, 省略时读取当前服务配置.
        previous: 观察修订前人工批准的标准或未冻结草案.
        affected_sources: 在旧标准上允许修订或新增检查的来源 ID.
        redraft_reason: 门禁 A 针对参考图标准给出的人工重拟理由.

    Returns:
        只有合法观察来源的标准草案, 尚未冻结.

    Raises:
        InvalidCriteriaOutputError: 模型返回的草案无法用于当前固定观察.
    """
    if not request.strip():
        msg = "整图标准需要非空用户要求"
        raise ValueError(msg)
    manifest, observations = read_observation_package(observation_dir)
    reference = package_asset(observation_dir, manifest.reference.path).read_bytes()
    # 送模图像必须与刚验证的观察快照对应, 包目录可被外部重新拷贝或替换.
    if hashlib.sha256(reference).hexdigest() != manifest.reference.sha256:
        msg = "参考 PNG 已与固定观察包分离"
        raise ValueError(msg)
    width, height = reference_size(reference)
    reference_url = "data:image/png;base64," + base64.b64encode(reference).decode("ascii")
    inputs = {
        "observations": observations.model_dump(mode="json"),
        "context_notes": manifest.context_notes,
        "user_request": request,
        "reference_size": {"width": width, "height": height},
    }
    if redraft_reason and redraft_reason.strip():
        inputs["redraft_reason"] = redraft_reason.strip()
    if previous is not None:
        if previous.requirements != request or previous.scope != "scene":
            msg = "旧标准必须属于同一整图要求"
            raise ValueError(msg)
        inputs["revision"] = {
            "affected_sources": list(affected_sources),
            "previous_checks": [
                {"dimension": row.id, "check": text, "source_ref": ref}
                for row in previous.dimensions
                for text, ref in zip(row.required_checks, row.source_refs, strict=True)
                if ref in affected_sources
            ],
            "instruction": "仅输出受影响来源的新增或替换项及区域。未涉及的维度仅填 reason。程序会保留旧标准中无关的检查、区域和人工设置。",
        }
    message = _message(inputs, reference_url)
    organizer = model if model is not None else build_model()
    try:
        draft = _organize(organizer, message)
        criteria = _compile_criteria(draft, request, previous, set(affected_sources), width, height)
    except (OutputParserException, ValidationError) as exc:
        raise InvalidCriteriaOutputError(str(exc)) from exc
    try:
        validate_criteria_sources(criteria, observations)
    except ValueError as exc:
        raise InvalidCriteriaOutputError(str(exc)) from exc
    return criteria


def _compile_criteria(
    draft: SceneCriteriaDraft, request: str, previous: ReviewRequest | None, affected: set[str], width: int, height: int
) -> ReviewRequest:
    if previous is not None:
        return _merge_criteria(previous, draft, affected, width, height)
    return ReviewRequest(
        scope="scene",
        target="整图",
        requirements=request,
        dimensions=draft.dimensions,
        protocol="visual_review_v2",
        coverage=_scene_coverage(draft.required_regions, width, height),
    )


def _merge_criteria(previous: ReviewRequest, draft: SceneCriteriaDraft, affected: set[str], width: int, height: int) -> ReviewRequest:
    updated = {item.id: item for item in draft.dimensions}
    if updated.keys() != {row.id for row in previous.dimensions}:
        msg = "观察修订的标准草案必须逐一覆盖六个维度"
        raise InvalidCriteriaOutputError(msg)
    dimensions = []
    unchanged = set()
    replacements = set()
    for old in previous.dimensions:
        proposed = updated[old.id]
        kept = [(text, ref) for text, ref in zip(old.required_checks, old.source_refs, strict=True) if ref not in affected]
        added = [(text, ref) for text, ref in zip(proposed.required_checks, proposed.source_refs, strict=True) if ref in affected]
        unchanged.update((old.id, text) for text, _ in kept)
        replacements.update((old.id, text) for text, _ in added)
        if not added and len(kept) == len(old.required_checks):
            dimensions.append(old)
            continue
        dimensions.append(_revised_dimension(old, proposed, kept, added))
    retained = [row for row in previous.coverage.required_regions if (row.dimension, row.check) in unchanged] if previous.coverage else []
    retained.extend(row for row in draft.required_regions if (row.dimension, row.check) in replacements)
    return ReviewRequest.model_validate(
        {
            **previous.model_dump(mode="python"),
            "dimensions": tuple(dimensions),
            "coverage": _scene_coverage(tuple(retained), width, height),
        }
    )


def _revised_dimension(
    old: DimensionCriteria, proposed: DimensionCriteria, kept: list[tuple[str, str]], added: list[tuple[str, str]]
) -> DimensionCriteria:
    checks = (*kept, *added)
    exclusions = old.excluded_checks if kept else proposed.excluded_checks
    if not checks and not exclusions:
        exclusions = old.excluded_checks or ("复核后该维度无适用必查项",)
    tolerances = old.tolerances if kept else ()
    if added:
        tolerances = tuple(dict.fromkeys((*tolerances, *proposed.tolerances)))
    return DimensionCriteria(
        id=old.id,
        required_checks=tuple(text for text, _ in checks),
        source_refs=tuple(ref for _, ref in checks),
        excluded_checks=exclusions,
        tolerances=tolerances,
    )


def _scene_coverage(regions: tuple[RequiredRegion, ...], width: int, height: int) -> ReviewCoverage | None:
    if not regions:
        return None
    canvas = PixelRegion(left=0, top=0, right=width, bottom=height)
    if any(not canvas.covers(item.region) for item in regions):
        msg = "整图标准的必查区域须位于固定参考 PNG 内"
        raise InvalidCriteriaOutputError(msg)
    return ReviewCoverage(reference_window=canvas, rendered_window=canvas, required_regions=regions)


def _organize(model: BaseChatModel, message: HumanMessage) -> SceneCriteriaDraft:
    options = {"method": "function_calling", "strict": True} if isinstance(model, ChatOpenAI) else {}
    runner = model.with_structured_output(convert_to_openai_tool(_SceneCriteriaSlots, strict=True), **options)
    output = runner.invoke([SystemMessage(content=ORGANIZER_PROMPT), message])
    if isinstance(output, dict) and "dimensions" in output:
        # 既有离线模型或旧集成返回的逐维合同仍须通过原有完整校验.
        return SceneCriteriaDraft.model_validate(output)
    return _SceneCriteriaSlots.model_validate(output).compile()


def validate_criteria_sources(request: ReviewRequest, observations: SceneObservations) -> None:
    """校验标准或人工修订中的必查项都能追溯到固定观察或用户要求."""
    allowed = observations.identities | {"user_requirement"}
    for item in request.dimensions:
        if len(item.source_refs) != len(item.required_checks) or not set(item.source_refs) <= allowed:
            msg = f"{item.id}: 每个必查项须绑定本轮观察 ID 或 user_requirement"
            raise ValueError(msg)


def _local_dimension(criterion: DimensionCriteria, source_refs: frozenset[str]) -> DimensionCriteria:
    retained = []
    excluded = list(criterion.excluded_checks)
    for check, source in zip(criterion.required_checks, criterion.source_refs, strict=True):
        if source in source_refs:
            retained.append((check, source))
        else:
            excluded.append(check)
    return DimensionCriteria(
        id=criterion.id,
        required_checks=tuple(check for check, _ in retained),
        source_refs=tuple(source for _, source in retained),
        excluded_checks=tuple(excluded),
        tolerances=criterion.tolerances if retained else (),
    )


class NoLocalReviewChecksError(ValueError):
    """当前冻结标准没有覆盖这个局部试验的必查项."""


def derive_local_review_request(
    scene_criteria: BoundReviewCriteria, probe: RiskProbe, observations: SceneObservations, target_region: PixelRegion, *, canvas: PixelRegion
) -> ReviewRequest:
    """从整图已冻结 V2 标准派生试验可查项, 其余明确排除."""
    scene = scene_criteria.request
    if scene.scope != "scene" or scene.protocol != "visual_review_v2":
        msg = "局部标准须从门禁 A 冻结的 V2 整图标准派生"
        raise ValueError(msg)
    validate_criteria_sources(scene, observations)
    sources = probe_target_refs(observations, probe.target_ids)
    if not canvas.covers(target_region):
        msg = "局部试验区域须位于复用的参考像素画布内"
        raise ValueError(msg)
    dimensions = tuple(_local_dimension(item, sources) for item in scene.dimensions)
    if not any(item.required_checks for item in dimensions):
        msg = "当前冻结标准没有覆盖这个局部目标的必查项"
        raise NoLocalReviewChecksError(msg)
    coverage = _local_coverage(scene.coverage, dimensions, target_region, canvas)
    return ReviewRequest(
        scope="local",
        protocol="visual_review_v2",
        target_kind="element" if probe.target_ids[0].startswith("E") else "feature",
        target=", ".join(probe.target_ids),
        requirements=scene.requirements,
        dimensions=dimensions,
        reference_region=target_region,
        rendered_region=target_region,
        conditions=scene.conditions,
        coverage=coverage,
    )


def _local_coverage(
    scene: ReviewCoverage | None, dimensions: tuple[DimensionCriteria, ...], target: PixelRegion, canvas: PixelRegion
) -> ReviewCoverage:
    inherited = {(item.dimension, item.check): item.region for item in scene.required_regions} if scene is not None else {}
    required = tuple(
        RequiredRegion(dimension=item.id, check=check, region=inherited.get((item.id, check), target))
        for item in dimensions
        for check in item.required_checks
    )
    if any(not canvas.covers(item.region) for item in required):
        msg = "局部试验保留的整图必查区域超出参考像素画布"
        raise ValueError(msg)
    return ReviewCoverage(reference_window=canvas, rendered_window=canvas, required_regions=required)


def bind_local_probe(
    package: PlanningPackage,
    sketch_id: str,
    probe: RiskProbe,
    scene_criteria: BoundReviewCriteria,
    target_region: PixelRegion,
    *,
    alternative_index: int | None = None,
    max_attempts: int = 3,
    solid_rgba: tuple[float, float, float, float] | None = None,
    proxy_context: str | None = None,
    baseline_glsl: str | None = None,
    patch_source: str | None = None,
) -> LocalProbeBinding:
    """以已审定目标和上下文固定试验, 机制与标准从上游制品派生."""
    from shader_deep.workflows.review import freeze_workflow_review  # noqa: PLC0415  # 复用整体工作流的 V2 冻结入口.

    if patch_source is not None:
        validate_patch_fragment(patch_source)
    observations = package.source.observations
    source = package.source
    scheme = effective_planning_sketch(
        package.report, observations, sketch_id, alternative_index=alternative_index, upstream_gaps=source.observation_manifest.gaps
    )
    methods = probe_mechanism_ids(observations, scheme, probe.target_ids)
    width, height = reference_size(package.source.reference_bytes)
    canvas = PixelRegion(left=0, top=0, right=width, bottom=height)
    criteria = freeze_workflow_review(derive_local_review_request(scene_criteria, probe, observations, target_region, canvas=canvas))
    return LocalProbeBinding(
        **probe.model_dump(),
        planning_sha256=package.content_sha256,
        dependency_sha256=probe_dependency_digest(
            observations, scheme, probe.target_ids, reference_sha256=source.observation_manifest.reference.sha256, request=source.request
        ),
        sketch_id=sketch_id,
        alternative_index=alternative_index,
        mechanism_ids=methods,
        width=width,
        height=height,
        time=package.source.constraints.time,
        target_region=target_region,
        criteria=criteria,
        max_attempts=max_attempts,
        solid_rgba=solid_rgba,
        proxy_context=proxy_context,
        baseline_glsl=baseline_glsl,
        patch_source=patch_source,
    )
