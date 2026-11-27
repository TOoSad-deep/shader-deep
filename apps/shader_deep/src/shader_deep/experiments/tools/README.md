# 调参实验工具

每个模型工具单独一个文件. 工具只包装已有能力, 不拥有浏览器、拼图算法或历史存储实现.

`capture_preview` 和 `compare_preview` 使用 `content_and_artifact`: 原始模型消息内容不变, 另在 `ToolMessage.artifact` 保存程序快照及对应图片身份. 执行层据此核对后续请求确实包含图片; artifact 不进入模型 HTTP 正文. 直接 `.invoke({})` 仍返回图片内容列表. 历史工具不生成当前观察回执.

工厂的 `reference_path` 兼容接受 `ParameterReference`, 可与任务消息共同使用一次冻结的 PNG. 工具自身仍可独立调用; Agent 的权限、顺序、预算和最终状态检查集中在 [_parameter_policy.py](../_parameter_policy.py) 及 [parameter_run.py](../parameter_run.py), 不在每个工具重复实现.

| 文件 | 职责 |
| --- | --- |
| [__init__.py](__init__.py) | 统一导出 `build_parameter_tools` |
| [factory.py](factory.py) | 读取并固定参考图、创建共享历史、装配工具 |
| [set_parameters.py](set_parameters.py) | 定义参数修改工具, 名称/范围校验交给预览 |
| [capture_preview.py](capture_preview.py) | 定义原始截图工具, 可选登记成功快照 |
| [compare_preview.py](compare_preview.py) | 定义并排观察工具, 委托图像模块拼图 |
| [get_trial_history.py](get_trial_history.py) | 定义原图或参考/历史并排回读工具, 不操作浏览器 |
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

`build_parameter_tools(..., history_mode="paired")` 显式启用实验性历史并排; 默认 `"original"` 保留独立历史原图. paired 必须提供 `reference_path`, 每条历史以左侧固定参考、右侧 `Trial N / revision R` 原尺度并排, 文本保留同一快照的完整参数、截图编号、参数版本和时间; `layout` 为 `reference_left_history_right`. 不传编号仍读取最近三张, 显式指定仍最多三张, 原始 PNG 不被覆盖. 标题位于图像区域之外, 不缩放、不裁剪; 尺寸不匹配时返回工具错误. 此模式不加入当前图, 不读取浏览器, 不登记新 trial, 也不隐式恢复参数; 当前状态仍通过 `compare_preview` 或 `capture_preview` 观察.

对比 original/paired 时应使用相同固定参考、trial 和参数轨迹, 并向两组初始输入提供相同参考图. paired 会在历史输出中重复参考图, 因而实验衡量的是参考/历史呈现方式及重复参考的整体影响, 不能单独归因为拼图布局. 工具不添加分数、排序或自动恢复; 模型须自行观察并决定下一次修改.

拼图实现在 [imaging/comparison.py](../../imaging/comparison.py); 历史存储实现在 [_parameter_history.py](../_parameter_history.py). 渲染仍由 [rendering](../../rendering/README.md) 提供. 这些工具不计算/发送 MAE、评分、排名或优劣结论. 参考图、截图编号、原尺度并排和历史生命周期的完整契约见[应用 README](../../../../README.md#只读视觉反馈工具).

错误边界: 入参格式、历史编号和 uniform 范围错误返回 `ToolMessage.status=error`, 模型可以修正后继续. 浏览器关闭、会话失效、绘制/导出故障保留 `RenderError` 并终止图, 避免反复请求无法恢复的工具. 历史仅保存成功快照; 旧截图仍可读取, 但不能证明浏览器已恢复. 完整实验建议使用 [run_parameter_task](../parameter_run.py), 统一管理请求预算和最终取图的追踪状态.
