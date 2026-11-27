# 调参历史展示实验的独立评估协议

## 目的和适用范围

本协议先固定评估方法, 再比较 `get_trial_history` 原有图片与并排图片两种展示方式. 调参器在各次运行中最多发出 18 次实际模型请求, 包含失败请求和重试. 评估只在运行结束后执行, 指标和独立评审结论不回传调参器, 避免把评估反馈引入 A/B 自变量.

`circles-parameter-evaluation-v2` 仅适用于本记录固定的双圆参考, 不能直接推广为任意图像评估器. 改参考、尺寸、ROI、指标语义或 rubric 时应建立新版本. 本文描述协议和本地验证, 实际模型运行结果由实验记录追加, 不以实现完成代替效果证据.

## 固定输入与空间区域

- 参考来源: `runs/run-circles-case-5686vir4/reference.png`, 原始尺寸 1024x576.
- 参考 PNG 字节 SHA256: `8431f0a97a9caae5bdd1ad7e0d6d82e94a604c5a53c49cb9abd659f3f761a41a`.
- 来源路径仅作本地追溯. `freeze_reference` 校验身份后保存不可变 PNG 字节, 后续不重新读取参考路径.
- 左上原点, 矩形使用 `[left, top, right, bottom]`, 右、下边界不包含. 不做缩放、配准、平移、候选自适应裁剪或分别对齐.

| 区域 | 原始像素范围 | 像素数 | 选择依据 |
| --- | --- | ---: | --- |
| full | `[0,0,1024,576]` | 589824 | 保留构图、背景和主体位置 |
| foreground | `[330,145,695,435]` | 105850 | 在只看参考图时覆盖两个圆的完整轮廓和少量抗锯齿边界余量; 后圆约 x337..571,y193..429, 前圆约 x452..687,y149..383 |
| overlap | `[450,190,580,385]` | 25350 | 固定覆盖参考中交叠透色与过渡; 是矩形诊断窗口, 包含部分非交叠像素, 不宣称是精确交集掩码 |
| background | foreground 的补集 | 483974 | 四个互不重叠矩形拼成的固定区域, 直接检测背景退化 |

前景和交叠窗口由参考图的几何观察确定, 不根据候选、误差分布或组别调整. `evaluate_render` 在每份结果中重述矩形、像素数和来源规则. 参考字节和候选 PNG 字节分别哈希, 再用协议版本和两者哈希计算 `pair_id`; 相同画面的不同编码文件也保持不同制品身份.

## 数值诊断

透明像素统一按白底合成后以 RGB 比较. MAE 为全部像素 RGB 三通道绝对差的均值 / 255; RMSE 为三通道平方差均值的平方根 / 255. 两者范围为 [0,1], 越低表示编码像素越接近. full、foreground、overlap 和 background 各自独立报告, 不用大面积背景稀释主体误差后推断视觉通过.

full、foreground、overlap 另外报告 `ssim_luma`. 精确变体是编码亮度 `Y=0.299R+0.587G+0.114B`, 7x7 uniform 窗口, 只平均完全位于区域内的有效窗口, 样本方差/协方差分母 N-1, K1=.01,K2=.03,data_range=255. 通过积分图计算窗口矩, 仅将浮点误差导致的微小负方差钳为零. 小于 7 像素的区域返回 null. background 是不规则补集, 不报告其 SSIM.

该配置对应 [scikit-image 文档中的 uniform/sample-covariance 变体](https://scikit-image.org/docs/stable/api/skimage.metrics.html#skimage.metrics.structural_similarity), 不是 Wang 原始 Gaussian 窗口配置, 也不声称与任意默认库调用等价. 无新增 NumPy、SciPy 或 scikit-image 依赖. 代码使用 Pillow 和标准库实现, 单测另以逐窗口直接中心化协方差计算验证非恒定输入.

所有指标均为诊断, 不设置未经人工标签校准的视觉通过阈值. SSIM 亮度项不能替代颜色位置评审; MAE 改善也不能证明边缘或透明感更好. 若要选数值最优历史图, 预先指定排序键, 例如 foreground.mae; 并列时保留最早历史编号, 且始终另列实际最终图. 不将数值最优图替换为模型最终提交图后报告为“最终结果”.

## 独立视觉判断

`build_judge_request` 只产生双图内嵌数据和固定 rubric, 不调用外部模型. 第一张是固定参考, 第二张是被评估渲染. 评审看不到组名、路径、调参器说明、参数、历史、数值误差或另一实验组的结果. `pair_id` 只绑定双图身份, 不包含可读组别. 调用方保存模型配置、原始回复、已解析结论及对应图像哈希.

四个必评维度在真实调参实验前冻结于 `_judge.py` 的 `RUBRIC`:

1. `geometry_layout`: 两圆各自直径、中心、相对位置、交叠面积及画布占比.
2. `overlap_transparency`: 前后关系、交叠透色、后圆在前圆内的可见性及与非交叠区域的区别.
3. `color_placement`: 后圆左青右蓝、前圆交叠紫色和左上粉色的位置、强度、扩散范围.
4. `edges_transitions`: 两圆外轮廓及抗锯齿, 尤其前圆非交叠外边界; 区分内部柔和过渡与轮廓过度模糊、光晕或描边.

各维度只能返回 `pass`、`needs_work` 或 `unassessed`, 必须提供至少一条原图矩形定位和具体观察. pass 也需给出一致区域的证据. 任一维度 needs_work 则 overall 为 needs_work; 否则任一维度 unassessed 则 overall 为 unassessed; 四项都 pass 才为 pass.

v2 在相同 rubric 和严格 schema 之外, 明确展示含四个 `{status, evidence}` 对象的完整 JSON 形状示例, 禁止把 criterion 直接写成 evidence 数组. 示例使用 unassessed 占位, 明示不是图片判断且必须根据实际双图重写. 提示每项只写一条简洁局部证据以避免冗长输出; 解析器继续接受合法的多条证据, 未放宽关键字段或校验.

`parse_judge_result` 使用 extra=forbid 和 strict 类型校验. 非 JSON、重复键、NaN/Infinity、缺字段、空证据、空白观察、越界坐标、未知状态、总体与分项矛盾、pair_id 不匹配一律得到 `overall=unassessed` 和解析错误, 不猜补内容. 结构校验能检查证据存在和坐标有效, 不能证明观察本身真实.

评审自对照 sanity check: 参考对自身应无显著差异; 固定初始渲染应能定位明显尺寸等差异. 这两个样本只能排查明显失灵, 不能建立接受阈值、误判率或人类一致性. 所有解析成功的视觉结论仍标记 `acceptance=uncalibrated`; 用户视觉验收另记.

## 状态与 A/B 对比口径

运行记录应分别保留以下字段, 不合并为一个“成功”:

| 事实 | 解释 |
| --- | --- |
| execution | 正常结束、请求/渲染失败、预算耗尽等执行结果 |
| actual requests | 实际模型请求数及上限; 预算耗尽不表示视觉通过 |
| final | 正常结束后的真实最终图及其双图哈希, 失败时如仅有最后成功截图需明确其身份 |
| best_by_numeric | 由预先固定指标在已留存历史中选出的图, 只表示该指标最优 |
| assessed visual verdict | 当前双图的独立 rubric 结论, 无有效回执则 unassessed |
| acceptance | 未校准的模型判断与用户接受分开记录 |

真实 A/B 保持参考、shader、参数范围、初值、任务、工具权限、模型配置、渲染时间/分辨率及 18 次请求上限相同. 唯一变化为历史工具返回的视觉展示. 所有重试、失败、耗尽和回退均保留; 不只选择正常或好看的样本. 独立评估在事后运行, 不改变调参过程或停止条件. 小规模实验只能支持当前样本和配置的观察, 不直接声称统计显著或普遍改进.

## 可复用调用

```python
from pathlib import Path
from shader_deep.experiments.parameter_evaluation import (
    build_judge_request, evaluate_render, freeze_reference, parse_judge_result,
)

spec = freeze_reference(Path("runs/run-circles-case-5686vir4/reference.png"))
metrics = evaluate_render(spec, Path("final.png"))
request = build_judge_request(spec, Path("final.png"))
# 独立评审必须将 request["response_format"] 作为提供方参数传给模型, 不能只将 schema 放入提示词.
# 实验入口通过 model.bind(response_format=request["response_format"]).invoke(...) 调用, 不将结果送回调参器.
verdict = parse_judge_result(raw_judge_text, request["pair_id"])
```

`ReferenceSpec` 内含 PNG bytes, `dataclasses.asdict(spec)` 不可直接 JSON 序列化. 持久化来源时显式记录 `spec.sha256` 和 `spec.source`; `request["messages"]` 是含双图 data URL 的模型输入, 路径不会进入盲评材料.

## 本地验证

2026-11-06, 在应用目录执行:

- `.venv/bin/python -W error -m unittest tests.unit_tests.agents.test_parameter_evaluation -q`: v1 的 14 项通过. 覆盖同图、黑白解析值、位移、改色、固定 ROI、独立逐窗口 SSIM、背景补集、透明合成、非 PNG、尺寸不匹配、参考/双图绑定、路径盲化和非法评审回执. v2 新增完整形状示例可严格解析、旧数组回执仍被拒绝两项, 结果在下方单列.
- `.venv/bin/ruff check src/shader_deep/experiments/parameter_evaluation tests/unit_tests/agents/test_parameter_evaluation.py`: 通过.
- `.venv/bin/ty check src/shader_deep/experiments/parameter_evaluation`: 通过.
- 用冻结参考与自身运行 `evaluate_render`: 约 1.018 秒, 全图/前景/交叠/背景 MAE=RMSE=0, SSIM 分别为 0.9999999999844204 / 0.9999999999950182 / 1.0. 细微误差来自浮点矩差计算. 该检查未请求模型, 不属于视觉评审校准.

本模块未调用真实外部模型, 未修改默认生成或调参流程. 全包检查和真实 A/B 的证据由主实验记录补充.

## v1 真实控制失败与 v2 修订

2026-11-06, 首次 v1 真实预检的制品保留在 `runs/run-history-display-20261106-18calls/`. 两次控制各只有一次实际模型请求, 尚未启动任何调参 A/B 运行:

- reference-self: 模型正常结束, 但四个 criterion 均直接输出 evidence 数组, 缺少每项 status/evidence 对象. 严格解析结果为 unassessed, 未根据 overall=pass 或视觉文字补造通过结论.
- initial-control: 输出在 8192 completion tokens 上限结束, `finish_reason=length`, 共使用 10624 tokens. 结果为 unassessed, 不把未完成回复当作有效初始图评审.

上述两次都不是成功的视觉 sanity check, 更不是校准样本. 原始请求、原始回复、失败回执和 v1 报告均保留, 不以 v2 重新解析旧回执或覆盖失败记录.

v2 的修复范围为提示词增加明确嵌套形状示例、要求简洁证据, 及实验入口为独立评审配置 16384 输出 token 上限和 90 秒请求超时. 调参过程继续使用 8192 输出 token、45 秒超时、18 次实际请求上限, A/B 自变量不变. 新版本须另建冻结目录并重新执行独立控制; v2 实现和离线验证不证明真实模型格式成功率或视觉评审质量已经改善. 实际预算及复测结果以新目录 suite 和 HTTP 请求证据为准.

v2 本地验证: 上述 scoped unittest 命令 16 项通过 (0.040 秒), scoped Ruff check、format check 和 ty check 均通过. 两项新增测试验证模型可见完整例子符合未放宽的解析契约, 且 v1 的无 status 数组形状仍为 unassessed. 本次修订未请求外部模型.


## 原生结构化输出修复及实际结果

v2 两个基本检查正常完成, 但六份正式结果评审全部未通过回执校验: 三次输出截断、三次 JSON 语法错误. 这些回执全部作为 unassessed 保留. 仅在提示词内放置 JSON schema 和示例不能保证提供方返回有效 JSON.

随后用 R1-B 的同一双图、同一消息、同一模型/提供方、默认推理配置和 16384/90 预算做单变量验证, 仅增加 OpenRouter 原生 `response_format={type:json_schema,json_schema:{name,strict:true,schema}}`. 单次验证获得有效 needs_work 回执. 保持这一条件, 对两个基本检查和其他五个结果各调用一次, 复用 R1-B 已有有效回执, 没有补跑调参. 八份原生格式回执均有效; 六个结果均被判为 needs_work. 原始失败回执与新回执分别保留, 不以新判断替换执行失败或用户验收状态.

当前 `build_judge_request` 返回 canonical `response_format`, 默认实验入口实际绑定该参数, 并核对 HTTP 载荷中的 schema/strict 未丢失. 新的实验清单标注 `execution_mode=native_json_schema_v3` 并冻结格式哈希. 数值/ROI/rubric/严格本地解析规则仍是 `circles-parameter-evaluation-v2`, 未因输出格式修复而放宽评判内容. 提供方协议依据见 [OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs).

这证明当前提供方和本批样本的结构化输出链路有效, 不证明评审语义完全正确. 两个基本检查不是人工校准数据集; `acceptance=uncalibrated` 仍保留. 工程子 agent 使用 gpt-6.1-sol/high, 调参和独立评审沿用 GLM/CoreWeave, 不混淆两者的模型或 token 统计.

完整六次结果、预算耗尽后的单独终态重建、调用成本和验证范围见[调参实验记录](parameter-agent-experiment.md)末尾的 2026-11-06 记录; 结构化汇总见[实验证据](evidence/history-display-20261106.json).
