"""生成任务的固定报告绑定, 内容读取由存储层负责."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from shader_deep.domain.tasks import TaskRecord


@dataclass(frozen=True, kw_only=True)
class GenerationBinding:
    """明确选择的报告内容身份, 不保存第二份可编辑方案.

    Attributes:
        report_path: 本次运行内的完整报告副本目录.
        content_sha256: manifest、五库正文与原图的内容摘要.
        reference_sha256: 固定原图字节的 SHA256.
        element_id: 本轮报告的目标元素.
        sketch_id: 显式选定的草图.
        alternative: 局部备选的零基索引, 空值表示默认方案.
    """

    report_path: str
    content_sha256: str
    reference_sha256: str
    element_id: str
    sketch_id: str
    alternative: int | None = None


@dataclass(frozen=True, kw_only=True)
class ScenePlanningBinding:
    """整图规划的固定来源与显式方案选择.

    Attributes:
        report_path: 本次生成捕获的正式规划包.
        content_sha256: 规划包的完整内容身份.
        observation_sha256: 所绑定观察版本的内容身份.
        reference_sha256: 固定原图的字节摘要.
        sketch_id: 本次选中的整图方案.
        alternative: 可选局部备选的零基索引.
        schema_version: 序列化后用于辨认绑定的协议.
    """

    report_path: str
    content_sha256: str
    observation_sha256: str
    reference_sha256: str
    sketch_id: str
    alternative: int | None = None
    schema_version: Literal["scene_planning_v1"] = "scene_planning_v1"


def validate_generation_task(task: TaskRecord) -> None:
    """校验报告绑定的形状及角色, 不访问文件或解释机制语义.

    Args:
        task: 待登记的任务, 旧任务可以不带绑定.

    Raises:
        ValueError: 非生成任务带绑定, 或绑定字段不合法.
    """
    binding = task.generation_binding
    if binding is None:
        return
    if task.role != "generation":
        msg = "Only generation tasks can bind a generation report"
        raise ValueError(msg)
    names = ("report_path", "sketch_id") if isinstance(binding, ScenePlanningBinding) else ("report_path", "element_id", "sketch_id")
    for name in names:
        if not getattr(binding, name).strip():
            msg = f"Generation binding requires {name}"
            raise ValueError(msg)
    hashes = (
        ("content_sha256", "reference_sha256", "observation_sha256")
        if isinstance(binding, ScenePlanningBinding)
        else ("content_sha256", "reference_sha256")
    )
    for name in hashes:
        if re.fullmatch(r"[0-9a-f]{64}", getattr(binding, name)) is None:
            msg = f"Invalid generation binding {name}"
            raise ValueError(msg)
    if isinstance(binding, ScenePlanningBinding) and binding.schema_version != "scene_planning_v1":
        msg = "Invalid scene planning binding protocol"
        raise ValueError(msg)
    option = binding.alternative
    if option is not None and (isinstance(option, bool) or not isinstance(option, int) or option < 0):
        msg = "Generation alternative must be a non-negative integer"
        raise ValueError(msg)
