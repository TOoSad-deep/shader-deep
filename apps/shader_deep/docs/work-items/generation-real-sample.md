# 简单图片真实模型测试

日期: 2026-10-03. 代码基点: `a15c928d`.

## 结论

本次真实样例未通过. 五库分析没有交付合法报告, 因此没有进入报告绑定后的生成流程. 另行执行的 PNG 生成对照完成了真实模型调用、WebGL2 渲染、预览回读和选择, 但背景、布局与模型自检存在明显偏差. 程序的 `completed` 不表示视觉复现通过.

## 输入与范围

- 原图: [shiny-rectancle.png](../../test_pic/shiny-rectancle.png), 1280 x 720, 黑底蓝青色发光圆角矩形.
- 原图 SHA-256: `4ee0da315723a6692a21fd12252978da4d6a6246dec7e9e508591e053bd88927`.
- 模式: `REFERENCE_RECONSTRUCTION`. 单 Pass GLSL ES 3.00 `mainImage`, 不使用纹理, 原图尺寸, `time=0`.
- 使用既有 OpenRouter 配置的 `z-ai/glm-5.3-flash`, 沿用既有 LangSmith 追踪. 第二次分析及 PNG 对照仅在进程环境设置 `OPENROUTER_REASONING_EFFORT=low`, 未修改 `.env` 或应用代码.
- 分析使用两个独立视角, 并行数 2, 主调用上限 12, 请求额外重试 0, 网络操作超时 120 秒; 主输出额度 8192, worker/整合输出额度 16384. 第一次 worker 调用上限 4, 第二次为 3. 流式网络超时不是整个请求的墙钟上限.
- PNG 对照最多 3 次渲染, 逻辑模型调用上限 5; SDK 内部网络重试不计入该逻辑计数.
- 本次没有形成阶段 02 报告输入的真实成功样例, 没有测试阶段 03 两方案比较或阶段 04 两元素组合, 没有用户视觉接受结论.

## 三次运行

本地制品根目录为 `apps/shader_deep/runs/simple-pic-20261003-c74pny/`, 由 Git 忽略. 以下路径相对该目录, 不作为仓库内可复现的固定 fixture.

| 运行 | 制品 | 结果 |
| --- | --- | --- |
| 现有默认推理配置的五库分析 | `analysis/run-cd6z5yvh`, `analysis-console.log` | 两 worker 首轮分别约 184/190 秒, `finish_reason=length`, 无工具提交. 在第二轮期间停止进程, 保留中间记录; 没有最终报告. 不能仅凭正文长度断定全部额度消耗在推理上. |
| `low` 推理的五库分析 | `analysis-low/run-b_4qfmm3`, `analysis-low-index.json`, `analysis-low-console.log` | 最终 `failed`, `report_dir=null`. 主 Agent 4 次、两个 worker 各 3 次逻辑模型调用, 均未形成合法探索提交. |
| 独立 PNG 生成对照 | `png-generation/run-m7qa6ae0`, `generation-console.log`, `output.glsl` | 3 次模型调用, 2 次成功渲染, 程序 `completed`, 模型选择 `candidate-001`. 两张实际 PNG 均为 1280 x 720. |

第二次分析中, 几何 worker 依次遭遇不完整 JSON、额外字段 `report.sketches.0.parameter_hints`、草图名称中未使用 `[[ID]]` 的 `neon_frame` 引用. 颜色 worker 三次工具参数均为不完整 JSON. 校验与有限修复反馈生效, 但模型未能完成合法提交; 自动重派沿用已耗尽的调用预算. 本次证据不足以归因为传输截断或报告到生成的实现缺陷.

## 真实渲染与视觉核对

PNG 对照选中候选为 `run-m7qa6ae0-c001`. `output.glsl` 与其实际渲染源码一致, 仅 CLI 附加末尾换行. 候选源码 SHA-256 为 `f60a666b9addac10ec8cf71120eecc4081aca326a04354bb6acfdbd9e98adbb7`.

人工查看本次实际预览发现: 背景与矩形中心均被蓝绿色大面积填充, 矩形比参考偏窄并偏右. 模型的选择说明却称“纯黑背景”和“对齐良好”, 与实际图像不符. 这些是本次审查观察, 不代替用户视觉验收.

选中 Shader 的 `outerGlow = exp(-max(-d, 0.0) * 4.5) * 0.55` 在轮廓外 `d > 0` 时恒为 0.55, 导致远离主体的背景仍被照亮. `inner = exp(max(d, 0.0) * -18.0)` 在轮廓内恒为 1, 也向中心区域加入亮度. 这是生成的候选代码问题, 本次未手工改写候选以替代模型结果.

直接读取原始 PNG 的 RGB 像素, 坐标使用左上原点:

| 像素 | 原图 | 选中候选 |
| --- | --- | --- |
| `(0, 0)` | `(0, 0, 0)` | `(12, 47, 153)` |
| `(1279, 0)` | `(0, 0, 0)` | `(22, 147, 134)` |
| `(640, 360)` | `(0, 0, 0)` | `(13, 72, 113)` |

原始预览、两个候选 GLSL、`run.json`、完整分析日志、`test-manifest.json` 与 `verification.json` 均保留在上述本地目录. 最小 WebGL2 浏览器预检通过, 见 `browser-preflight.log`.

## 后续优先级

1. 先用此次失败记录定位结构化输出的稳定性, 让同一简单图能够产出合法且有可用草图的五库报告.
2. 再跑报告输入入口, 检查原图/方案/候选预览的实际请求内容与模型自检质量, 重点保留黑底、位置和轮廓这些明确要求.
3. 单方案真实样例可用后再进行两方案比较与两元素组合验收. 本次不增加评分系统、Agent 数量或新的渲染架构.
