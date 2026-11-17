# 十图重测结果 Review（2026-11-11）

结论: 文件交付和追踪可靠, 当前观察质量仍不足以作为稳定的下游输入. 背景覆盖改善, 关系归属、修复时的内容保持及视觉事实仍是主要风险. 本轮仅审阅, 未改运行代码、skill 或模型报告, 未新增模型测试.

## P1: 合同修复会丢失观察内容

粉圆轨迹显示: 第一次提交有 3 条 R、无 gaps; 工具返回 features.F1 重号. 第二次修改了编号, 却省略 relations 并新增一个 gaps, 随后被接受. 初稿同时使用 E2-F1 等不符合当前合同的关系引用, 且关系内容本身也不是已验收答案; 不能简单恢复初稿或只比条数. 这里证明的是修复前后出现语义内容丢失, 不是初稿完全正确.

粉圆、Windows、发光图均出现按元素重启 F 编号的冲突. 当前报错定位到首个 ID 冲突, 未给出所有冲突位置与受影响引用, 完整重提依赖模型自行维护. 建议先改善冲突反馈及修复范围, 再对修复前后的观察覆盖差异做有限复核. 有歧义的引用不能自动猜测重映射.

## P1: 已观察到的关系被留在 F, 或被重复登记

四份 partial 的最终原始工具参数都省略 relations 字段, 保存包按默认值成为空列表. 磨砂 F5 写覆盖变色, S 图 F2 写随形, 极光 F6 写上下对应; 这些关键配合并非完全没有看见. 在有 R 的报告中, 网格 F5/F8 与 R1、水面 F4 与 R1、双链 F5 与 R2 又有重复. 这是归属执行不稳定, 单看 R 的数量无法解决.

## P1: 方向、拓扑和明暗事实影响复现

S 图的复杂外轮廓被改写为六边形徽章, 会改变主要几何结构. 网格覆盖区线条实际变淡发糊, 报告写成加深. 磨砂图增加原图未见的左上短条, 并把被覆盖的蓝圆轮廓称为完整清晰. 发光图本轮没有再次明说心形正置, 但仍遗漏倒置方向; 这是遗漏, 与上一轮直接看反需区别.

## P2: gaps 与阶段必要性仍不一致

四份 partial 的缺口分别围绕软影精确范围、紫色准确色阶、逐条刻线和逐项水纹细节. 当前任务允许定性描述, 这些声明不足以说明必要目标缺失. 同时它们确实漏了关系, 所以清空 gaps 也不能把报告视为通过. 应先修必要内容, 再按任务要求判断是否还有缺口.

## 十图逐项意见

优先级按对当前观察产物的影响划分, 不是代码安全等级; 替代拆法不因条数不同而被判错. 当前规则明确要求背景为 E, 因而不再用旧版“背景只能放 context”的口径扣分.

| Case | 重点 | 意见 | 原报告与轨迹 |
| --- | --- | --- | --- |
| pink | P1 | 修复编号时从 3 条 R 变为省略整个 relations; 最终仍漏随轮廓配合, 把软影精确范围当缺口。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/pink/run-m497mfih/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82a-ed92-7482-9f32-fbddceccf455?poll=true) |
| windows | P2 | 背景与色带对应保留; 碎片群拆成多个分布属性, F/R 有重复; R3 的整体反差与已有背景/布局信息增量有限。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/windows/run-uucuyg5t/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82c-ef58-7e80-9795-969218218580?poll=true) |
| frosted | P1 | F5 已看见透显与覆盖变色, 却无 R; 变软观察不足; 凭空增加左上角短条, 蓝圆可见轮廓被说成清晰完整。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/frosted/run-8lajl1oh/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82c-ee31-7552-b465-926deb51209f?poll=true) |
| balls | P2 | 背景、面板与单一小件群结构较合适, 柔影和布局均在; 额外咬合/叠压关系依据不足, 群体与单件模板的引用不清楚表达相邻对象。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/balls/run-4n0e8rhy/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82c-e9dc-7352-9531-484f72ee4918?poll=true) |
| liquid-retry | P1 | F5/F8 与 R1 重复交互; 格线变淡发糊被写成加深, 错位/弯折断言偏强; 光源方向由外观直接推断。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/liquid-retry/run-rux_yuj3/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82d-6be1-7353-9765-4a3aad333679?poll=true) |
| cool-s-distance | P1 | 把复杂 S 外轮廓概括为六边形徽章, 内部连续随形带简化成分段刻线; F2 已写随形却无 R。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/cool-s-distance/run-haft5kn4/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82d-e792-7fa3-a1da-c36c8b4b5f10?poll=true) |
| flat-water-effects2 | P2 | 前景波纹变大变柔和描述改善; F4 与 R1 重复反光, 反光向近处逐渐变淡缺乏充分依据; 海平线独立 E 的价值可讨论。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/flat-water-effects2/run-puqxsb8c/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82e-0734-75a2-8130-a048d141cfa1?poll=true) |
| dna-helis | P2 | 黑底、节点和连线配合覆盖; 节点大小变化被弱化, 亮暗周期描述过粗; F5 与 R2 重复长短配合。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/dna-helis/run-aapiptaa/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82e-9c57-7090-8988-f5ccc05a145f?poll=true) |
| buffer-bloom | P1 | 斜向布局存在; 未明确心形尖端朝上、双瓣朝下, 光晕交叠仍漏; 背景 F2 重复各图形光晕, R2 主要重复各自外观。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/buffer-bloom/run-8l_hb63n/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82e-b197-7d40-b43d-531374c923f9?poll=true) |
| auroras | P1 | 主要分区和竖纹保留; F6 明写倒影与上方对应, 却无 R; 逐项水纹细节被当成必要缺口。 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-observer-traced-20261111/auroras/run-3yb31r7y/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c82f-370c-7021-ab68-7bbe4ea63342?poll=true) |

## 轨迹证明与不能证明的部分

- 十条轨迹可远程回查, 原图进入模型输入; 本轮未发现图片未送入模型的证据.
- 仅小件群读取过两份参考, 其余九图未读. 当前 skill 明确按疑难触发, 这不是单凭读次数可认定的违规, 也不能推出“多读就会更好”.
- 磨砂、S、极光第一轮就省略 relations; 粉圆是在重提时丢失. 两条失败路径需要分别处理.
- 业务拒绝可能返回 accepted=false 而工具状态仍为 success. 粉圆轨迹 error_runs=0 并不表示没有业务校验失败, 审阅应看工具返回内容.
- 当前合法提交即结束, 没有结构通过后的语义复核. 这是已采用的合同边界, 不能把文件合法当成视觉验收.

## 建议优先级

1. 先保护修复内容: 精确定位编号冲突与关联引用, 限制修复范围, 检查观察覆盖是否丢失.
2. 固定评价项, 单独实验一次同 Agent 语义复核: 只问关键关系是否漏报、主要归属是否重复、缺口是否必要. 不强制 R 数量, 不增加独立 Agent.
3. 视觉事实单独评测: 比较原图回看或模型能力的收益, 不用粒度提示修正所有感知错误.
4. 保留背景 E 与当前轻量合同. 用未参与规则设计的新图检查泛化, 不继续堆叠十图专属答案.

本次已经确认问题, 尚未证明上述建议的效果. 相同模型的一次重跑、改过的 skill 与旧版报告不能构成严格因果对照.

轨迹审阅摘录保存在本目录 trace-review.json; 仅包含调用元数据、提交列表是否存在及工具返回摘要, 不保存模型隐藏推理.
