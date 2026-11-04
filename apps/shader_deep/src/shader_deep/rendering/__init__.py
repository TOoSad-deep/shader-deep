"""独立 WebGL2 单帧渲染和常驻预览接口."""

# 调用方只需要渲染器和统一异常, 不需要知道 Playwright 页面或 JavaScript 的细节.
# 此包不依赖 Agent、黑板或模型, 可单独传入 Shader 代码测试渲染结果.

from shader_deep.rendering.models import ParameterSpec, PreviewSnapshot, RenderError
from shader_deep.rendering.preview import ShaderPreview
from shader_deep.rendering.renderer import WebGL2Renderer

__all__ = ["ParameterSpec", "PreviewSnapshot", "RenderError", "ShaderPreview", "WebGL2Renderer"]
