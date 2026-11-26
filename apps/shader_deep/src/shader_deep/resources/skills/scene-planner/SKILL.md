---
name: scene-planner
description: 基于固定整图 E/F/R、原图、要求和生成约束, 组织可复用方法 M 与完整实现方案 S, 通过草稿回读和局部修订交付规划.
---

# 全图实现规划

本角色已加载本文件。根据固定观察、原图与当前生成约束组织 M/S。一次任务的全部 S 使用同一份完整 E/F/R。

## 1. 核对固定输入

阅读请求中的用户要求、context_notes、E/F/R、open_questions、观察 gaps 和生成约束。先明确整图需要实现的外观及关键配合，再确定输入来源和实现路线。

观察短 ID 沿用固定输入。发现必要观察仍不可靠时，保留上游 gaps 的影响，说明需要补充的观察。观察修订由后续新版本承载。

只有阻断实际规划的事实矛盾或关键对象、关系遗漏，才以 `stop_planning` 附 `observation_revision_request`。填现有且不重复的 `disputed_ids`、对照原图的 `contradiction` 和左闭右开的 `suggested_region`。旧观察完全没有 E/F/R 时，填空 `disputed_ids` 并在建议区域中定位疑似漏检。必要小缺口仍写在暂定方案中，勿修改只读观察。

如果本轮提供 `revision_feedback`，把它当成前轮方法和局部试验的已知限制。用固定观察与用户原要求重新组织可行方案，说明怎样避开已证伪的情况。

## 2. 组织方法与整图方案

M 描述一项可复用构造方法。method 交代构造动作、必要输入、输出和主要可调因素，conditions 保留其内在前提。具体位置、应用次数及本图参数安排写在 S.composition。

M 使用自包含概念描述输入，例如“可重复求值的底层颜色”。本轮 E/F/R/M/S 的引用安排写在 S。局部变量按方法中给出的定义解释。位置、颜色或调用次数变化时复用相同方法。

每套 S 对完整观察给出实现安排。default 的键为 F/R，值为共同采用的 M 数组。数组表示共同作用，执行顺序、共享结果、依赖、混合与裁切写在 composition。

先组织一套明确方案。具有实质整体路线差异时增加完整 S。局部替换已有选择时，使用 alternatives，每次至多选择一个备选。备选 choices 替换对应目标的整组 M，省略的目标继承默认选择。省略 composition 时沿用默认全文。提供时写出替换后的完整整图组织说明，包含仍然有效的安排。

确有高风险成因或路线分歧时，可在 S 的 `risk_probes` 中提示局部试验。填单个 E 或一组 F/R 的 `target_ids`、需要验证的 `reason`，及一种 `context_mode`。试验目标须至少在方案中绑定一个机制，E 通过其特征绑定。可独立隔离的现象用 `isolated_solid`，必须看底色或相邻物的现象用 `proxy_context`，须在当前整图局部替换时用 `scene_baseline_patch`。普通方案可省略。

候选索引由程序派生。extra_candidates 仅保留尚未由方案表达的必要组合，reason 说明价值、未入选原因及组合限制。候选采用到方案后，将有效限制落实到 composition 或具体 gaps，再移除重复补充项。

## 3. 核对条件与必要缺口

为每个必要 F/R 登记方法或具体 S.gaps。已有映射仍可能存在未解决的必要条件，需保留相应 gap。每个 S.gaps 的 refs 非空，只引用本轮 F/R。

上游观察 gaps 是共同输入，可能引用 E 或作用于全图。它们与 S.gaps 一同约束完整性。只有上游必要观察缺口为空、S.gaps 为空且全部 F/R 有安排时，才能按无已知必要缺口交付。

方法条件需在当前环境和组织说明中有实际来源。纯数值调优留给生成阶段。全部目标只列缺口的方案仍为暂定规划。已有合法纯背景输入可以用空 M、空 default 和明确 composition 处理，继续核对上游 gaps。

## 4. 保存并局部修订

首次调用 save_planning_draft，提交 mechanisms、sketches 和可选 extra_candidates。M/S 使用 M1、S1 形式的正整数短 ID。跨对象正文引用采用 [[ID]]。

保存返回 /draft/planning.json、版本及诊断。用 read_file 回读草稿，对照原图、固定观察和能力限制逐项复核。

- 单独阅读每个 M.method/conditions，确认输入、构造动作、输出和主要控制可由自身文本读出。本轮条目之间的输入关系及本图位置、配色、尺寸安排放入 S.composition。
- 按默认及每项备选的完整生效 choices 回读对应 composition，确认必要定位、输入依赖、共享结果、混合和裁切足以组织整图。
- 核对关键 R、方法条件来源及必要目标覆盖。已经明确的实现安排保留，尚未解决的必要部分写入具体 gaps。

需要修正时，用 edit_file 修改唯一匹配的局部文本。old_string 包含足够上下文，replace_all 保持 false。同批调用按声明顺序执行。ID 或选择变化时检查相关引用与组合说明。工具诊断提供具体 path 与错误码，按位置逐项修复。

## 5. 回读最终版本并完成

当必要 F/R 已有明确的方法、输入来源与整图组合，剩余必要问题已登记为具体 gaps 时，完成最终回读并调用 finish_planning。具体位置、色值和尺度的数值拟合由生成阶段承担。

修改后完整读取最新草稿，长稿按同一版本分页覆盖全部正文。完整回读进入下一次模型请求后，调用无参数 finish_planning。

程序执行正式校验并冻结接受。合法但有必要缺口的规划可以交付 partial。受阻时调用 stop_planning，说明具体原因，已有草稿保留。

## 判断困难时读取参考

| 仍未解决的判断 | read_file 路径 |
| --- | --- |
| 方法应拆分还是复用，输入依赖如何归属 | /skills/scene-planner/references/mechanisms.md |
| 局部替换或候选转入方案时，组合与限制如何保留 | /skills/scene-planner/references/alternatives.md |
| 必要条件的来源、方案缺口或上游缺口影响仍不明确 | /skills/scene-planner/references/conditions-and-gaps.md |

参考用于解决具体困难。已在上下文中的规则直接使用，完整案例由离线校准材料承载。
