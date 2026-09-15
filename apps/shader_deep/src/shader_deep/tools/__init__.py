"""生成任务的工具包, 保留会话、运行结果与预算异常的统一导入入口."""

# session.py 是模型工具的绑定入口; render.py / finish.py 是普通 Python 执行逻辑.
# 对外仅暴露会话和结果类型, Agent 无需自己管理底层浏览器.

from shader_deep.tools.session import RenderSession
from shader_deep.tools.types import GenerationLimitError, GenerationOutcome

__all__ = ["GenerationLimitError", "GenerationOutcome", "RenderSession"]
