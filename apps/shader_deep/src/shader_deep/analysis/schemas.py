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

    def __post_init__(self) -> None:
        """拒绝重复条目标识、不存在的观察引用及同时支持和反对的引用."""
        identifiers = [item.id for item in (*self.observations, *self.interpretations)]
        observations = {item.id for item in self.observations}
        if len(set(identifiers)) != len(identifiers):
            msg = "Report item IDs must be unique"
            raise ValueError(msg)
        for item in self.interpretations:
            supports, opposes = set(item.supporting_observation_ids), set(item.opposing_observation_ids)
            if not (supports | opposes) <= observations or supports & opposes:
                msg = (
                    f"Invalid observation references in interpretation {item.id}: "
                    f"missing={sorted((supports | opposes) - observations)}, "
                    f"both_supporting_and_opposing={sorted(supports & opposes)}; "
                    f"available_observation_ids={sorted(observations)}"
                )
                raise ValueError(msg)


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class SourceRef:
    """引用报告内的具体条目; item_id 为空时引用整份报告."""

    result_id: Text
    item_id: Text | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class SourcedStatement:
    """保留报告来源与测量证据的综合陈述."""

    text: Text
    source_refs: Annotated[tuple[SourceRef, ...], Field(min_length=1)]
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
    disagreements: tuple[SourcedStatement, ...] = ()
    open_questions: tuple[SourcedStatement, ...] = ()
    implementation_hints: tuple[SourcedStatement, ...] = ()
    missing_task_ids: tuple[Text, ...] = ()
