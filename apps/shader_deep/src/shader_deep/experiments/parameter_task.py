"""构建以参考图为目标的调参输入, 不向模型提供目标参数或本地文件路径."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from shader_deep.infrastructure.llm.messages import png_data_url

if TYPE_CHECKING:
    from pathlib import Path


def build_reference_task(reference_path: Path, *, requirement: str = "") -> HumanMessage:
    """把目标 PNG 的原始内容装入用户消息, 用于直接调用调参 Agent.

    Args:
        reference_path: 本地目标 PNG, 构建消息时读取并固定内容, 不缩放或裁剪.
        requirement: 可选补充要求, 例如优先匹配轮廓; 不应包含隐藏目标参数.

    Returns:
        标注目标参考图身份并包含实际图片内容的多模态消息.

    Raises:
        ValueError: 文件不存在或扩展名不是 PNG, 沿用公共图片读取约定.
        OSError: 无法读取图片文件.
    """
    # 路径只在调用方进程内使用; 不把可能含参数答案的文件名发给模型.
    reference_url = png_data_url(reference_path)
    instruction = (
        "下面的图片是只读的目标参考图, 不是当前渲染图. "
        "任务是调整工具允许的 uniform 参数, 使当前渲染尽量与参考图一致. "
        "若有 compare_preview, 用其左参考右当前的并排图观察基线和每次修改结果; "
        "否则调用 capture_preview 后与本参考图对照. "
        "按 `差异 → 参数 → 修改后验证` 操作: 先指出具体差异及预期变化, "
        "通过 set_parameters 提交新值, 再单独取图检查该差异是否改善. "
        "只修改允许参数, 不修改 Shader 源码、参数范围或参考图. "
        "结束前依据最后一次修改后的最新截图复查主要差异, 报告最终参数、截图 revision 和剩余问题. "
        "不假定知道目标参数; 未完成最新截图验证时如实说明, 不声称已匹配."
    )
    if requirement.strip():
        instruction += "\n补充要求: " + requirement.strip()
    return HumanMessage(content=[{"type": "text", "text": instruction}, {"type": "image_url", "image_url": {"url": reference_url}}])
