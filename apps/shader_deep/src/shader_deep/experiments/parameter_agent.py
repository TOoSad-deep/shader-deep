"""可独立调用的 Shader 调参 Agent 实验入口."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents import create_deep_agent

from shader_deep.experiments._parameter_model import CompleteParameterResponse, configure_parameter_model, connection_retry
from shader_deep.experiments.tools import build_parameter_tools
from shader_deep.infrastructure.llm.client import build_model

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph

    from shader_deep.rendering import ShaderPreview


def build_parameter_agent(
    *,
    preview: ShaderPreview | None = None,
    model: BaseChatModel | None = None,
    render_tool: BaseTool | None = None,
    reference_path: Path | None = None,
) -> CompiledStateGraph:
    """构建独立调参 Agent, 模型沿用项目环境变量配置.

    Args:
        preview: 外层管理的常驻会话, 绑定调参及取图工具.
        model: 可选的聊天模型, 未提供时沿用项目环境变量配置.
        render_tool: 绑定固定模板、参数白名单和渲染条件的调参工具.
            仅保留旧单工具调用兼容, 不能和 preview 同时提供.
        reference_path: 可选只读参考 PNG, 配合 preview 启用并排对比和截图历史.

    Returns:
        可直接通过 invoke 或 ainvoke 调用的 Agent 图.

    Raises:
        ValueError: 配置冲突、参考图无效, 或项目模型配置无效.
        OSError: 参考图无法读取.
    """
    if preview is not None and render_tool is not None:
        msg = "Provide preview or render_tool, not both"
        raise ValueError(msg)
    if reference_path is not None and preview is None:
        msg = "reference_path requires preview"
        raise ValueError(msg)
    tools = build_parameter_tools(preview, reference_path=reference_path) if preview is not None else []
    if render_tool is not None:
        tools.append(render_tool)
    return create_deep_agent(
        name="shader-parameter-tuner",
        model=configure_parameter_model(build_model() if model is None else model),
        tools=tools,
        middleware=[connection_retry(), CompleteParameterResponse()],
        system_prompt=(
            "你负责调整已有 Shader 的 uniform float 参数, 根据用户目标提出参数修改. "
            "只调整用户指定且工具允许的参数, 保持算法和结构不变. "
            "若任务包含目标参考图, 以匹配该图为目标; 参考图只读, "
            "capture_preview 返回的是当前渲染, 不能将两者混淆. "
            "有 compare_preview 时优先用它观察基线和修改结果, 并排图左侧是参考图、右侧是当前图. "
            "按 `差异 → 参数 → 修改后验证` 逐轮操作, 每一步只需简短说明观察和决定.\n"
            "差异: 先取图观察基线, 列出最明显的一至三项具体差异, 如整体偏小、位置偏移、边缘偏虚. "
            "除非用户指定其他重点, 优先处理轮廓、整体大小和位置, 再处理颜色、亮度与光晕. "
            "不要仅凭参数值推断画面已经匹配.\n"
            "参数: 每次提交前说明要改善哪项差异、选择哪些允许参数以及预期的可见变化. "
            "只修改与该差异有关的参数, 通过 set_parameters 提交; 未提供的参数保留当前值.\n"
            "修改后验证: 每次成功更新后, 单独调用 compare_preview 或 capture_preview, "
            "读取返回的实际图像, 核对完整参数和 revision 是否对应刚才的修改. "
            "针对本轮要解决的差异, 判断已改善、无明显变化、变差或无法判断, 并说明剩余差异或新问题. "
            "完成这一步后才决定下一次调整, 不把参数更新成功当成视觉改善. "
            "更新和取图应分轮调用, 不在同一轮并行发出.\n"
            "结束前检查: 必须已经读取并检查最后一次成功修改后的最新截图, 包括恢复旧参数后的新截图. "
            "用该图逐项复查基线发现的主要差异, 报告各项是否仍存在或无法判断, "
            "给出最终参数、所依据截图的 revision 和剩余差异. "
            "已检查最新截图且之后没有修改时, 可以直接据此总结, 不必重复取图. "
            "不能用较早版本的截图或参数回执替代末次修改后的验证, 也不能把有所改善说成完全匹配. "
            "预算用尽、取图失败或无法判断时, 如实报告尚未匹配或尚未完成验证, 不编造完成结论.\n"
            "若已配置 get_trial_history, 可按需回读历史参数和截图, 再自行决定是否用 set_parameters 恢复; "
            "历史回读不是必做步骤, 也不能替代最新状态的检查. "
            "这些工具不提供评分, 不编造数值误差或排名; trial_id 表示成功取图顺序, 不等于 revision 参数版本. "
            "如果无法辨认图像内容, 明确说明观察受限, 不凭元数据编造图像描述或断言工具漏图. "
            "缺少必要输入时先询问, 未配置预览工具时只提出建议, 不声称效果已经验证."
        ),
    )
