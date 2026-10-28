# 以开发简洁性为目标的框架复评

> 历史记录: 下文保留当时的实现、命令与验证结果. 旧分析执行和回放已退出当前包, 最新范围见[清理记录](analysis-legacy-cleanup-20261028.md).

后续用户明确选择“能用 Deep Agents 就优先使用框架, 有真实缺口再自定义”. 当前实施方向以[Deep Agents 原生优先方案](deep-agents-native-plan-20261028.md)为准; 下文保留此前 create_agent 候选的分析, 不作为待执行决定.

- 日期: 2026-10-28.
- 状态: 只读评估完成, 未修改业务实现; 本轮未运行测试或真实模型.
- 新判据: 少维护重复规则、减少阅读跳转、让角色与工具修改集中. 性能、token 和真实模型成功率不作为本轮框架选型的主要判据.
- 本文调整[首轮评估](framework-evaluation-20261028.md)的推荐优先级, 不撤销其中的实验事实和局限.

## 1. 当前建议

**优先设计所有当前分析角色统一用 LangChain `create_agent`, 主角色按需挂载独立的 SkillsMiddleware; 同时精简应用自己的执行封装. 不把“worker 用一个工厂、主角色用另一个工厂”作为长期目标.**

这是一项基于源码的设计推荐, 尚未验证统一工厂后的主角色运行. 上一轮 51 项候选测试只替换 worker, 不能拿来证明本轮的新主角色方案已经通过.

Deep Agents 依赖暂不要求移除: 仍可复用它的 SkillsMiddleware 和 FilesystemBackend, 旧兼容流程也可能使用其完整工厂. 统一的是当前分析的装配方式, 不是为了依赖列表更短而重写 skill.

SDK `task` 暂不作为简化方案. 当前派发是固定小批次, 换成 `task` 后还得包回任务身份、固定输入、提交回执和恢复规则, 不能直接消掉这些自维护代码.

## 2. 为什么不再要求主角色保留完整 Deep Agent

[SkillsMiddleware](../../../../libs/deepagents/deepagents/middleware/skills.py)直接继承 LangChain AgentMiddleware, 自带 SkillsState, 接收 backend 与 sources, 并在 before_agent 加载元数据. 这些能力不要求由 `create_deep_agent` 才能调用.

项目的 OrchestrationSkill（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/skill.py`, 已删除）已经自己创建 backend、SkillsMiddleware 和受限 `read_skill` 工具. 当前工厂（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/execution.py`, 已删除）另外装配完整 Deep Agents, 随后关闭摘要、替换主角色文件中间件, 再通过应用中间件收窄提示词和工具.

更直接的目标是:

| 角色 | 明确装配的能力 |
| --- | --- |
| 编排主角色 | create_agent + SkillsMiddleware + 业务工具 + 必要请求/提交约束 |
| 探索、整合等子角色 | create_agent + 本角色工具 + 同一套必要请求/提交约束 |

可以从当前装配路径撤掉完整 harness 的默认文件工具、默认 general-purpose 子 agent, 以及“安装摘要再关闭摘要”的配置. FilesystemBackend 留给 skill 读取, 不必因此给模型增加通用文件能力.

MainAgentLoop._with_context（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/orchestrator_loop.py`, 已删除）目前先清理框架系统说明, 又手工注入 skill 说明. 采用明确装配后, 可以安排 skill 注入只发生一次. 必须保留“元数据被发现”“全文读到”“实际进入请求”“本轮开放工具”的区分, 但不必保留今天的重复接线方式.

## 3. 真正值得删减的代码

### A. 默认执行不再绕经兼容构造类 — Strong

当前继承链:

```text
MainAgentLoop
  → FiveAnalysisLoop
    → AnalysisLoop
      → AnalysisMiddleware
```

证据: 五库循环（历史路径 `src/shader_deep/runtime/five_analysis_loop.py`, 已删除）、兼容运行器（历史路径 `src/shader_deep/runtime/runner.py`, 已删除）、当前装配（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/execution.py`, 已删除）.

`AnalysisLoop` 增加的是旧 `run()` 构造方式. 当前 worker 自己装配图并调用公共驱动; 主角色也覆盖了 `run()`. 这层父类对默认执行的阅读路线没有贡献.

建议让当前约束直接建立在 AnalysisMiddleware 上, 将旧 `run()` 能力留在兼容入口. 迁移时检查旧 FiveAnalysisLoop 的直接调用方, 不能因内部改继承就丢失仍被使用的入口.

收益是 locality: 理解当前角色少追一层旧构造实现, 而不是把旧构造复制到新的父类. 先做这个窄改动, 不急于将所有中间件再拆成许多小策略类.

### B. 配置优先级计算只留一次 — Strong

目前工作流先解析角色选项, 角色入口又解析一次. runtime.resolve_role_options（历史路径 `src/shader_deep/runtime/options.py`, 已删除）和[workflows.resolve_phase_options](../../src/shader_deep/workflows/configuration.py)还各自实现一遍阶段/CLI 优先级.

原先用提前解析解决跨层依赖, 实际留下了新的重复. 推荐以公共角色执行入口作为规范化位置: 工作流只传原始配置; 入口负责一次解析, 核心优先级算法只有一份. 旧解析函数只做返回形式与来源标签的兼容适配.

ResolvedRoleOptions 应只表达本角色已选定的值; 没必要继续携带其他阶段的备选输出值和整份来源信息, 让它看起来仍像“待解析配置”. 来源记录需要的内容单独由解析结果提供.

收益是 leverage: 新增一个选项或角色时只维护一处算法. 不要求调用方分辨“原配置”“已解析但仍可解析的配置”. 这是对上一轮“宿主提前解析 + 角色兼容再次解析”方案的主动收敛, 不是声称原实现有已证实的优先级错误.

### C. 将重复的执行接线收进同一个 module — Strong

run_main_agent（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/orchestrator.py`, 已删除）和run_role（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/execution.py`, 已删除）仍重复配置模型、审计、预算检查、恢复范围、扣费、活跃性检查和运行配置.

这部分应由一个执行 module 持有. 角色只负责自己的提示词、材料、工具和特有权限规则; 主角色增加 skill 即可. 隐藏这些共同接线才会增加 module 的 depth.

不建议把现有长参数列表换成一个无边界的字典, 也不建议设计注册所有角色、工具和生命周期的通用框架. 如果新接口仍要求调用方传入所有预算与审计回调, 只是换了包装, 并没有简化.

## 4. 用日常开发操作检查方案

| 开发动作 | 当前阅读负担 | 整理后的目标 |
| --- | --- | --- |
| 增加一个普通探索工具 | 找角色工具列表, 同时理解公共装配是否还会添加/过滤 SDK 默认能力 | 只在角色拥有的工具处接入; 请求与执行授权按同一工具集合处理 |
| 增加一种需要证据回读的工具 | 还要看 WorkerEvidence 的呈现与发布 | 保留证据相关修改, 不把真实业务约束当作无用代码删除 |
| 修改阶段预算 | 两套优先级函数、工作流预解析和角色再解析 | 一套解析规则与明确来源结果 |
| 新增或调整角色 | 需要辨别 run_role、MainAgentLoop.run、兼容 AnalysisLoop.run | 当前角色只有一条执行接线与消息驱动路线 |
| 修改报告契约 | 领域模型、校验、读取/生成消费者可能同时受影响 | 保持集中领域定义; 必要消费者联动不应被“少改文件”的指标掩盖 |

## 5. 哪些代码不能仅因为长就删除

- **公共消息驱动:** create_agent 自己能循环调用工具, 但不会自动知道 TaskStore 是否接受了业务提交. 目前外层“未有效提交则继续”的行为需要保留或明确替代, 不能看到两个循环就删一个.
- **提交校验与结果持久化:** 决定哪些结果可以交付, 是应用本身的职责.
- **实际呈现与引用约束:** 目录和路径不能替代模型收到图片或正文; 工具增多时这项规则仍有价值.
- **模型服务适配:** 原始工具参数、流式完整性和 reasoning 回传来自实际服务行为, 换装配工厂不会消除这些需求.

上述职责可以在 interface 后集中实现, 不必暴露成每个角色都要理解的一组回调.

## 6. 更大删减需要明确的产品取舍

| 可进一步删减的能力 | 能减少什么 | 接受的变化 |
| --- | --- | --- |
| 独立恢复决策 agent | recovery 角色、专用任务、父任务等待/授权关系和一部分额度协调 | 固定有限尝试后记录失败, 不再让模型决定最后一次重派 |
| 旧四库与固定五库执行支持 | 历史角色、运行器和相关兼容维护 | 旧调用/回放不再由当前包保证, 需要明确退出范围 |
| 跨进程任务恢复 | 一部分 Attempt 代次、恢复投影和重开状态规则 | 中断后从新运行重新分析; 仍保留结果文件与失败日志 |

这些属于可讨论的简化选择, 本次偏好修正不等于已经决定删除. 如果只保留现有行为, 本轮首先做 A/B/C 与统一工厂; 若允许缩减这些能力, 才可能进一步显著减少总体代码. 不承诺一个未经实现核对的删行比例.

## 7. 推荐顺序和验收口径

1. 收拢重复配置与执行接线, 将兼容父类移出默认阅读路线.
2. 当前主、子角色统一 `create_agent`; 主角色显式挂载已有 SkillsMiddleware, 避免自建另一套 skill 系统.
3. 仅在确有当前用途时保留默认路径的能力; 历史兼容通过薄入口进入旧实现.
4. 再根据实际使用情况决定是否退出旧协议、模型恢复决策和跨进程恢复.

验收以源码为主: 一个配置规则只定义一次; 当前角色构造只走一条路线; 新工具无需理解默认 harness 的额外工具; 不再有只为转发而引入的新抽象.

实施时复用少量已有用例核对提交、工具权限、独立上下文与主角色 skill 门禁, 再执行项目必要检查. 不要求重新证明毫秒、token 或视觉质量收益, 不为每个类或文件新增测试. 本轮只读评估没有运行这些验证.
