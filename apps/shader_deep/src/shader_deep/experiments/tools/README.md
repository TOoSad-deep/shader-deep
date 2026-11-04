# 调参实验工具

每个模型工具单独一个文件. 工具只包装已有能力, 不拥有浏览器、拼图算法或历史存储实现.

| 文件 | 职责 |
| --- | --- |
| [__init__.py](__init__.py) | 统一导出 `build_parameter_tools` |
| [factory.py](factory.py) | 读取并固定参考图、创建共享历史、装配工具 |
| [set_parameters.py](set_parameters.py) | 定义参数修改工具, 名称/范围校验交给预览 |
| [capture_preview.py](capture_preview.py) | 定义原始截图工具, 可选登记成功快照 |
| [compare_preview.py](compare_preview.py) | 定义并排观察工具, 委托图像模块拼图 |
| [get_trial_history.py](get_trial_history.py) | 定义历史回读工具, 不操作浏览器 |
| [_common.py](_common.py) | 共用图片消息格式和预期错误反馈设置 |

新代码统一从本包导入:

```python
from shader_deep.experiments.tools import build_parameter_tools

# preview 由外层 with ShaderPreview(...) 管理, reference 是本地 PNG Path.
tools = {operation.name: operation for operation in build_parameter_tools(preview, reference_path=reference)}
blocks = tools["compare_preview"].invoke({})
history = tools["get_trial_history"].invoke({"trial_ids": [1]})
```

不提供 `reference_path` 时仅装配 `set_parameters`、`capture_preview`; 提供时再追加 `compare_preview`、`get_trial_history`. 每个文件的 `build_tool` 接收已经准备的依赖, 返回一个工具; 调用方通常使用统一工厂, 保证截图与历史共享同一记录对象. 工具顺序、输入与消息格式保持兼容, 旧 [parameter_tool.py](../parameter_tool.py) 仅做导入转发.

拼图实现在 [imaging/comparison.py](../../imaging/comparison.py); 历史存储实现在 [_parameter_history.py](../_parameter_history.py). 渲染仍由 [rendering](../../rendering/README.md) 提供. 这些工具不计算/发送 MAE、评分、排名或优劣结论. 参考图、截图编号、原尺度并排和历史生命周期的完整契约见[应用 README](../../../../README.md#只读视觉反馈工具).

错误边界: 入参格式、历史编号和 uniform 范围错误返回 `ToolMessage.status=error`, 模型可以修正后继续. 浏览器关闭、会话失效、绘制/导出故障保留 `RenderError` 并终止图, 避免反复请求无法恢复的工具. 历史仅保存成功快照; 旧截图仍可读取, 但不能证明浏览器已恢复. 完整实验建议使用 [run_parameter_task](../parameter_run.py), 统一管理请求预算和最终取图的追踪状态.
