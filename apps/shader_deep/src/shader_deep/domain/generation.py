"""生成任务的固定报告绑定, 内容读取由存储层负责."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    for name in ("report_path", "element_id", "sketch_id"):
        if not getattr(binding, name).strip():
            msg = f"Generation binding requires {name}"
            raise ValueError(msg)
    for name in ("content_sha256", "reference_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", getattr(binding, name)) is None:
            msg = f"Invalid generation binding {name}"
            raise ValueError(msg)
    option = binding.alternative
    if option is not None and (isinstance(option, bool) or not isinstance(option, int) or option < 0):
        msg = "Generation alternative must be a non-negative integer"
        raise ValueError(msg)
