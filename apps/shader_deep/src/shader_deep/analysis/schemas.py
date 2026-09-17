"""经过运行时校验的不可变视角配置与分析报告结构."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints
from pydantic.dataclasses import dataclass

from shader_deep.analysis.evidence import EvidenceRequest, ImageRegion  # noqa: TC001  # Pydantic 需要在运行时解析这些字段的结构.

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
# 与业务黑板的标准库 dataclass 不同, 这里的 Pydantic dataclass 会执行运行时校验.
# 非空文本先去首尾空白; 未知字段直接拒绝, 防止模型悄悄扩展报告协议.
CONFIG = ConfigDict(extra="forbid")
SourceKind = Literal["report", "observation", "interpretation", "visual_element", "visual_feature", "visual_relation"]


class AnalysisValidationError(ValueError):
    """携带可直接用于局部修复的字段路径, 仍兼容原 ValueError 入口."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        """保留完整错误列表, 文本异常仍可用于既有日志与调用方."""
        self.issues = issues
        super().__init__("; ".join(f"{issue['path']}: {issue['message']}" for issue in issues))


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class SourceRef:
    """引用报告内的具体条目; item_id 为空时引用整份报告."""

    result_id: Text
    item_id: Text | None = None
    kind: SourceKind | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class HypothesisLink:
    """综合假设继承哪些原始解释, 与陈述的观察依据分开记录."""

    hypothesis_id: Text
    derived_from: Annotated[tuple[SourceRef, ...], Field(min_length=1)]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualEvidence:
    """视觉条目的依据; 缺少来源时保留为未经独立核对的初始判断."""

    basis: Literal["visual", "measurement_supported"] = "visual"
    evidence_ids: tuple[Text, ...] = ()
    uncertainties: tuple[Text, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualElement(VisualEvidence):
    """中性命名的可定位元素, 不直接认定材质或代码图层."""

    id: Text
    name: Text
    region: Text
    region_box: ImageRegion | None = None
    parent_id: Text | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualFeature(VisualEvidence):
    """允许属于多个元素的可见特征."""

    id: Text
    element_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    description: Text
    region: Text
    region_box: ImageRegion | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualRelation(VisualEvidence):
    """多个元素之间的可观察组织关系."""

    id: Text
    element_ids: Annotated[tuple[Text, ...], Field(min_length=2)]
    description: Text


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualDecomposition:
    """可修订视觉结构; 未提供结构使用 None, 不等同于空结构."""

    elements: tuple[VisualElement, ...] = ()
    features: tuple[VisualFeature, ...] = ()
    relations: tuple[VisualRelation, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualRevision:
    """针对已有或报告新增对象的修订建议, 不覆盖原始结构."""

    target_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    proposal: Text


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualMapping:
    """从初稿或报告局部对象到综合对象的映射; 空来源表示初稿."""

    source_id: Text
    target_id: Text
    kind: Literal["element", "feature", "relation"]
    source_result_id: Text | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class RenderCheckpoint:
    """后续渲染应比较的可见现象及需要保护的结构."""

    region: Text
    compare: Text
    preserve: tuple[Text, ...] = ()
    region_box: ImageRegion | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class ImplementationSketch:
    """关联明确假设的可选实现方向, 不代表已验证的生成参数."""

    id: Text
    hypothesis_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    description: Text
    element_ids: tuple[Text, ...] = ()
    feature_ids: tuple[Text, ...] = ()
    adjustable_variables: tuple[Text, ...] = ()
    render_checkpoints: tuple[RenderCheckpoint, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class LensDraft:
    """模型提出的视角内容; 标识和来源由执行器补齐."""

    # focus 限定观察范围; method_notes 描述方法; default_questions 提供视角的默认问题.
    name: Text
    focus: Text
    method_notes: tuple[Text, ...] = ()
    default_questions: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class LensConfig(LensDraft):
    """与单个独立任务绑定的完整视角快照."""

    id: Text
    origin: Literal["preset", "generated"]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class AnalysisTaskRequest:
    """一个分析任务请求, 通过预设 ID 或新建视角二选一指定分析方式."""

    objective: Text
    preset_id: Text | None = None
    lens: LensDraft | None = None
    # 追加分析的报告输入显式列出, 首轮要求为空, 避免独立视角先互相影响.
    related_result_ids: tuple[Text, ...] = ()
    purpose: Literal["initial", "supplement", "verify"] = "initial"
    # gap 解释为何需要追加任务, expected_evidence 说明希望补到什么依据.
    # 非空要求依赖是否为首轮, 由 AnalysisSession 在派发时检查.
    gap: Text | None = None
    expected_evidence: Text | None = None
    evidence_ids: tuple[Text, ...] = ()
    focus_element_ids: tuple[Text, ...] = ()
    focus_feature_ids: tuple[Text, ...] = ()

    def __post_init__(self) -> None:
        """确保预设视角与自定义视角恰好提供一个."""
        if (self.preset_id is None) == (self.lens is None):
            msg = "Provide exactly one of preset_id or lens"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Observation:
    """可见现象的观察记录, 含报告内唯一标识和图像位置描述."""

    id: Text
    text: Text
    region: Text
    # region 是文字定位; region_box 可选, 不能可靠确定坐标时只保留文字描述.
    region_box: ImageRegion | None = None
    basis: Literal["visual", "measurement_supported"] = "visual"
    evidence_ids: tuple[Text, ...] = ()
    element_ids: tuple[Text, ...] = ()
    feature_ids: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Interpretation:
    """对现象的解释, 通过同一报告中的观察记录提供支持或反证."""

    id: Text
    text: Text
    # 引用只在本报告内有效; 跨报告引用通过综合阶段的 SourceRef 表达.
    supporting_observation_ids: tuple[Text, ...] = ()
    opposing_observation_ids: tuple[Text, ...] = ()
    uncertainties: tuple[Text, ...] = ()
    verification_question: Text | None = None
    element_ids: tuple[Text, ...] = ()
    feature_ids: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class LensReport:
    """子任务撰写的分析内容; 结果 ID 和执行状态由程序赋予."""

    scope: Text
    observations: Annotated[tuple[Observation, ...], Field(min_length=1)]
    kind: Literal["lens_report"] = "lens_report"
    interpretations: tuple[Interpretation, ...] = ()
    uncertainties: tuple[Text, ...] = ()
    suggestions: tuple[Text, ...] = ()
    # 子任务只提出取证需求, 协调器读到报告后决定是否实际测量或追加复核.
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    visual_additions: VisualDecomposition | None = None
    visual_revisions: tuple[VisualRevision, ...] = ()
    implementation_sketches: tuple[ImplementationSketch, ...] = ()

    def __post_init__(self) -> None:
        """拒绝重复条目标识、不存在的观察引用及同时支持和反对的引用."""
        seen: set[str] = set()
        issues: list[dict[str, object]] = []
        for group in ("observations", "interpretations"):
            for index, item in enumerate(getattr(self, group)):
                if item.id in seen:
                    issues.append({"path": f"/{group}/{index}/id", "code": "duplicate_report_id", "message": "Report item IDs must be unique"})
                seen.add(item.id)
        # 条目身份尚不唯一时不继续推断观察引用, 避免同一 ID 对应多个含义.
        if issues:
            raise AnalysisValidationError(issues)
        observations = {item.id for item in self.observations}
        issues = [issue for index, item in enumerate(self.interpretations) for issue in _interpretation_reference_issues(item, index, observations)]
        if issues:
            raise AnalysisValidationError(issues)


def _interpretation_reference_issues(item: Interpretation, index: int, observations: set[str]) -> list[dict[str, object]]:
    """一次列出全部可独立修正的本地观察引用, 路径相对报告本身."""
    conflicting = set(item.supporting_observation_ids) & set(item.opposing_observation_ids)
    issues: list[dict[str, object]] = []
    for field in ("supporting_observation_ids", "opposing_observation_ids"):
        missing = set(getattr(item, field)) - observations
        conflict = conflicting if field == "opposing_observation_ids" else set()
        if missing or conflict:
            message = (
                f"Invalid observation references in interpretation {item.id}: "
                f"missing={sorted(missing)}, both_supporting_and_opposing={sorted(conflict)}; "
                f"available_observation_ids={sorted(observations)}"
            )
            issues.append({"path": f"/interpretations/{index}/{field}", "code": "invalid_observation_reference", "message": message})
    return issues


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class SourcedStatement:
    """保留报告来源与测量证据的综合陈述."""

    text: Text
    source_refs: Annotated[tuple[SourceRef, ...], Field(min_length=1)]
    id: Text | None = None
    element_ids: tuple[Text, ...] = ()
    feature_ids: tuple[Text, ...] = ()
    basis: Literal["visual", "measurement_supported"] = "visual"
    evidence_ids: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class AnalysisSummary:
    """分析阶段的综合输出; 未完成任务的 ID 由执行器填写."""

    source_result_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    # 关键观察、假设、分歧和实现建议分开保存, 综合不应抹去独立报告中的不确定性.
    key_observations: Annotated[tuple[SourcedStatement, ...], Field(min_length=1)]
    kind: Literal["analysis_summary"] = "analysis_summary"
    relationships: tuple[SourcedStatement, ...] = ()
    hypotheses: tuple[SourcedStatement, ...] = ()
    hypothesis_links: tuple[HypothesisLink, ...] = ()
    disagreements: tuple[SourcedStatement, ...] = ()
    open_questions: tuple[SourcedStatement, ...] = ()
    implementation_hints: tuple[SourcedStatement, ...] = ()
    missing_task_ids: tuple[Text, ...] = ()
    visual_decomposition: VisualDecomposition | None = None
    visual_mappings: tuple[VisualMapping, ...] = ()
    implementation_sketches: tuple[ImplementationSketch, ...] = ()
