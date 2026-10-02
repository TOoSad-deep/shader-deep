"""两套固定方案的比较记录, 不承载子运行的候选正文或会话状态."""

from __future__ import annotations

from dataclasses import dataclass

PLAN_COUNT = 2


@dataclass(frozen=True, kw_only=True)
class GenerationPlan:
    """调用方明确选择的一套草图方案.

    Attributes:
        name: 比较时显示的方案名称.
        sketch_id: 固定报告中的草图 ID.
        alternative: 可选局部备选的零基索引.
    """

    name: str
    sketch_id: str
    alternative: int | None = None


@dataclass(frozen=True, kw_only=True)
class ComparisonItem:
    """一项独立生成的结果索引, 详细结果保留在源运行.

    Attributes:
        plan: 本项显式方案.
        run_dir: 实际子运行目录.
        task_id: 子运行中的生成任务 ID.
        stop_reason: 子运行的实际结束原因.
        selected_candidate_id: 子运行选中的候选, 未完成时为空.
        code_path: 已选候选的实际 GLSL 路径.
        preview_path: 实际可用的预览路径.
        error: 运行异常诊断, 没有异常时为空.
    """

    plan: GenerationPlan
    run_dir: str
    task_id: str
    stop_reason: str
    selected_candidate_id: str | None = None
    code_path: str | None = None
    preview_path: str | None = None
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class GenerationComparison:
    """共同执行条件、两项结果及独立的人工选择.

    Attributes:
        directory: 比较索引的目录.
        report_sha256: 共同报告内容身份.
        reference_sha256: 共同参考图字节身份.
        element_id: 本次比较的同一元素.
        request: 共同用户目标.
        background: 共同背景要求.
        baseline_id: 共同起始候选, 无基线时为空.
        model_name: 固定模型名称, 不保存连接凭据.
        width: 固定渲染宽度.
        height: 固定渲染高度.
        time: 固定渲染时间.
        max_attempts: 每项的渲染尝试上限.
        items: 两项独立生成的结果索引.
        selected_item: 用户选择的零基条目索引, 未选择时为空.
        selection_reason: 用户提供的可选选择理由.
    """

    directory: str
    report_sha256: str
    reference_sha256: str
    element_id: str
    request: str
    background: str
    baseline_id: str | None
    model_name: str
    width: int
    height: int
    time: float
    max_attempts: int
    items: tuple[ComparisonItem, ...]
    selected_item: int | None = None
    selection_reason: str | None = None

    def __post_init__(self) -> None:
        """保持两项比较与人工选择的必要业务约束."""
        validate_generation_plans(tuple(item.plan for item in self.items))
        if self.selected_item is not None:
            select_comparison_item(self, self.selected_item)


def validate_generation_plans(plans: tuple[GenerationPlan, ...]) -> None:
    """验证首版比较只接收两套显式方案.

    !!! warning "实验性接口"
        当前仅支持两项比较, 不扩展为任意批次规划器.

    Args:
        plans: 调用方选定的方案.

    Raises:
        ValueError: 方案数量不是两项.
    """
    if len(plans) != PLAN_COUNT:
        msg = "Generation comparison requires exactly two plans"
        raise ValueError(msg)


def select_comparison_item(comparison: GenerationComparison, item_index: int) -> ComparisonItem:
    """解析可被人工选择的已完成条目, 不读取其运行文件.

    !!! warning "实验性接口"
        只验证比较记录的业务条件, 子运行一致性由存储入口检查.

    Args:
        comparison: 已形成的两项比较记录.
        item_index: 用户指定的零基条目索引.

    Returns:
        已完成且具有实际选择的条目.

    Raises:
        ValueError: 比较尚未结束、索引无效, 或条目尚未完成并选择候选.
    """
    if any(item.stop_reason not in {"completed", "blocked", "attempt_limit", "model_limit", "error"} for item in comparison.items):
        msg = "Comparison selection requires two finished runs"
        raise ValueError(msg)
    if isinstance(item_index, bool) or not isinstance(item_index, int) or item_index not in range(len(comparison.items)):
        msg = "Comparison item index is out of range"
        raise ValueError(msg)
    item = comparison.items[item_index]
    if item.stop_reason != "completed" or item.selected_candidate_id is None:
        msg = "Comparison item has no completed selection"
        raise ValueError(msg)
    return item
