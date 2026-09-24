"""黑板中的业务记录及任务绑定, 文件内容和会话历史由各自的存储承载."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

if TYPE_CHECKING:
    from shader_deep.analysis.evidence import MeasurementRecord
    from shader_deep.analysis.exploration import ExplorationReport, PossibilityLibrary, VisualOutline
    from shader_deep.analysis.schemas import AnalysisSummary, LensConfig, LensReport

# 这些类型描述业务记录, 不是模型 Tool 的参数 schema.
# Literal 和 TypedDict 提供静态类型约束, 不会自动校验外部 JSON.
# generation 和独立多视角 analysis 使用同一黑板; review 尚未接入.
AgentRole = Literal["analysis", "generation", "review"]
ResultStatus = Literal["completed", "partial", "blocked"]


# frozen=True 禁止直接重新赋值记录字段; kw_only=True 要求用字段名传参.
# 更换目标或任务要求时创建新记录, 从而保留旧任务当时使用的输入.
@dataclass(frozen=True, kw_only=True)
class TargetRecord:
    """一个固定版本的用户目标.

    Attributes:
        version: 在当前黑板中唯一的目标版本.
        request: 用户提出的要求.
        reference_path: 参考图的制品路径, 此处不加载图像.
        constraints: 输出环境等约束.
        protected_features: 后续实验需要保护的特征.
    """

    version: str
    request: str
    reference_path: str
    constraints: tuple[str, ...] = ()
    protected_features: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TaskRecord:
    """一次任务的固定要求和输入引用.

    Attributes:
        id: 在当前黑板中唯一的任务标识.
        role: 执行本任务的角色.
        target_version: 本任务使用的目标版本.
        objective: 本次要解决的问题或完成的判断.
        baseline_id: 起始候选版本, 首次分析或生成时可以没有基线.
        candidate_ids: 本任务明确指定的其他候选输入, 如待评审版本.
        related_result_ids: 本任务明确选入的历史结果.
        experiment_id: 本任务所属实验的标识.
        hypothesis: 本次待验证的假设.
        allowed_changes: 本次允许修改的范围.
        stop_conditions: 本次任务的结束条件.
        parent_task_id: 分析子任务所属的主分析任务.
        lens_config: 本次独立分析使用的完整视角快照.
        analysis_purpose: 首轮、补充或定向复核, 均属于分析模块.
        analysis_gap: 追加任务要解决的具体缺口及其影响.
        expected_evidence: 本次追加任务准备取得的新依据.
        evidence_ids: 明确选入本次分析子任务的程序证据.
    """

    id: str
    role: AgentRole
    target_version: str
    objective: str
    # 基线是本任务的起点, 不随后续生成候选或选择结果自动切换.
    baseline_id: str | None = None
    # 显式引用决定材料范围: 黑板里存在某条记录, 不等于本任务要使用它.
    candidate_ids: tuple[str, ...] = ()
    related_result_ids: tuple[str, ...] = ()
    experiment_id: str | None = None
    hypothesis: str | None = None
    allowed_changes: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    parent_task_id: str | None = None
    lens_config: LensConfig | None = None
    analysis_purpose: Literal["initial", "supplement", "verify"] = "initial"
    analysis_gap: str | None = None
    expected_evidence: str | None = None
    evidence_ids: tuple[str, ...] = ()
    # 新探索任务绑定统一初稿, 不向模型暴露整个 TaskRecord.
    analysis_outline: VisualOutline | None = None


@dataclass(frozen=True, kw_only=True)
class CandidateRecord:
    """生成任务产出的一个候选及其制品引用.

    Attributes:
        id: 在当前黑板中唯一的候选版本.
        task_id: 来源生成任务, 通过它追溯目标和起始基线.
        code_path: 本候选的代码路径.
        preview_path: 已有预览的路径, 尚未渲染时为空.
        limitations: 实现限制或尚未验证的内容.
    """

    id: str
    task_id: str
    # 此处保存路径, 实际文件由工具写入、Context 读取; 创建记录不会触发 I/O.
    code_path: str
    preview_path: str | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ResultRecord:
    """一次任务提交的业务结果, 与版本采用决定分开保存.

    Attributes:
        id: 在当前黑板中唯一的结果标识.
        task_id: 来源任务, 用于追溯角色和实验条件.
        status: 本次提交的完成情况, 不代表候选已被采用.
        summary: 本次完成的工作.
        candidate_ids: 结果所依据的候选, 包括指定输入、关联历史结果的证据和本任务产出.
        observations: 实际观察到的现象.
        hypotheses: 对原因的假设或待验证解释.
        limitations: 未解决事项和判断不确定性.
        recommendation: 建议的后续动作.
        analysis_detail: 结构化分析报告或综合结果; 分析内容以此字段为准.
    """

    id: str
    task_id: str
    # completed 描述一次结果提交的完成情况, 不等于候选已采用或用户已验收.
    status: ResultStatus
    summary: str
    candidate_ids: tuple[str, ...] = ()
    # 将事实、解释和建议分开保存, 便于后续 Agent 判断哪些结论需要再验证.
    observations: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    recommendation: str | None = None
    analysis_detail: LensReport | AnalysisSummary | ExplorationReport | PossibilityLibrary | None = None
    analysis_protocol: str | None = None


class BlackboardState(TypedDict):
    """单个项目的业务状态, 由调用方持有并接收黑板函数返回的新状态."""

    # 四张以 ID 为键的字典; 这是应用业务状态, 不是 LangGraph 的 messages 历史.
    # 字典本身可变, 不可变更新约定由 blackboard.py 的登记函数维护.
    targets: dict[str, TargetRecord]
    tasks: dict[str, TaskRecord]
    candidates: dict[str, CandidateRecord]
    results: dict[str, ResultRecord]
    # 可选字段, 用于兼容既有生成黑板和已保存的快照.
    measurements: NotRequired[dict[str, MeasurementRecord]]


@dataclass(frozen=True, kw_only=True)
class TaskRecords:
    """按任务引用读取的业务材料, 尚未经过模型上下文的选材与编码.

    Attributes:
        task: 本任务的固定输入要求.
        target: 本任务绑定的目标版本.
        baseline: 本任务绑定的起始候选.
        inputs: 本任务明确指定的其他候选输入.
        outputs: 本任务已登记的候选产出.
        related_results: 本任务明确选入的历史结果.
        results: 本任务已经提交的结果记录.
    """

    # read_task 将 ID 引用解析成对象, 形成一次读取结果.
    # 此对象不会随着黑板后来增加记录而自动刷新, 下一轮需要重新读取.
    task: TaskRecord
    target: TargetRecord
    baseline: CandidateRecord | None
    inputs: tuple[CandidateRecord, ...]
    outputs: tuple[CandidateRecord, ...]
    related_results: tuple[ResultRecord, ...]
    results: tuple[ResultRecord, ...]
