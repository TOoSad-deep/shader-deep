"""生成工具共享的运行结果与预算异常类型."""

# 将返回类型独立出来, Agent 与会话可以共用定义, 无需让类型模块导入执行逻辑.

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.tasks import BlackboardState, CandidateRecord


@dataclass(frozen=True, kw_only=True)
class GenerationOutcome:
    """一次生成运行的业务结果.

    Attributes:
        state: 已登记候选和结果的最新黑板.
        run_dir: 保存代码、预览与 run.json 的目录.
        selected_candidate: 本轮已渲染并由模型选择的候选, 未完成时为空.
        stop_reason: completed、attempt_limit、model_limit 或 error.
        attempts: 已执行的候选尝试次数.
        model_calls: 模型调用轮数, 不包含客户端内部网络重试.
    """

    # outcome 返回最新业务快照; selected_candidate=None 时也可查看失败尝试与停止原因.
    # frozen 约束字段重赋值, 不会递归冻结 state 内部的字典.
    state: BlackboardState
    run_dir: Path
    selected_candidate: CandidateRecord | None
    stop_reason: str
    attempts: int
    model_calls: int


class GenerationLimitError(RuntimeError):
    """运行已达到配置的尝试或模型轮数上限."""

    # 由预算中间件抛出, 在 run_generation 中作为可预期的退出信号捕获.
