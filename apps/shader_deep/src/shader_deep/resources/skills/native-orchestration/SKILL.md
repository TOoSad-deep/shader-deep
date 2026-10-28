---
name: native-orchestration
description: 多视角分析的主角色使用, 负责登记元素、冻结方向、调用原生 task 和核对权威结果.
---

1. 根据固定原图与要求定位元素, 使用 submit_elements 登记元素目录. 所有 feature_ids 为空, 特征由各探索方向独立观察.
2. 选择一个已登记目标, 使用 freeze_plan 一次固定范围和本批方向. 方向数量取当前输入的 max_tasks, 通常三个. 每个方向描述观察重点, 保留对机制的开放判断. 可选 target_box 使用原图整数坐标, 右下不含, 覆盖完整目标和必要上下文.
3. 使用 SDK task(description=调用意图, subagent_type=explorer-1 或 explorer-2 或 explorer-3), 完成本批每个已冻结槽位. 可同批调用, 每个槽位只有一次执行; 目标和方向由程序固定, description 不能修改.
4. 等本批所有探索形成成功或失败记录后, 使用 task(description="整合本批合法结果", subagent_type="integrator") 请求汇集与整合. 合法 V0 会在整合前由程序固定; 整合失败保留 V0 和真实缺口.
5. 根据 task 的权威回执, 需要正文时调用 read_analysis_result(result_id). 普通文本、todos 或 scratch 文件不能代替提交. 有必要时读取外置回执的完整正文. 核对后结束主会话, 程序计算最终状态与报告包.

原生 task 仅选择已冻结的 explorer 槽位或 integrator. 失败槽位保留失败原因, 其他合法结果仍可交付. 每个角色仅在自己的 scratch 中记录笔记, 业务输入和产物以程序回执为准.
