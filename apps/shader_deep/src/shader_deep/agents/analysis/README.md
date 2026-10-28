# 分析角色

## 职责

将主角色、独立探索和整合装配为 Deep Agents 图, 定义各角色的提示、工具及请求材料. SDK 执行工具循环、task 委派、摘要和 scratch 文件操作; 接受报告、任务终态和版本选择由[分析工作流](../../workflows/analysis/README.md)负责.

## 阅读入口

1. [native.py](native.py) 的 `build_analysis_agent`: 从主图装配了解角色、工具和中间件.
2. 同文件的 `_child`: 固定输入投影 → `create_deep_agent` 子图 → 权威回执投影.
3. [observations.py](observations.py) 的 `ReferenceObservations`: 原图聚焦、按需取证、呈现登记和证据字节冻结.

角色方法分别在[编排 skill](../../resources/skills/native-orchestration/SKILL.md)、[探索 skill](../../resources/skills/native-exploration/SKILL.md)和[整合 skill](../../resources/skills/native-integration/SKILL.md). 修改方法时读对应一份即可.

## 输入、输出与状态

| 角色 | 固定输入 | 业务工具与输出 |
| --- | --- | --- |
| 主角色 | 原图、要求、槽位容量及本轮进度 | 登记元素、冻结方向、读取合法结果; 用 SDK `task` 委派 |
| `explorer-1/2/3` | 冻结目标、原图、范围及自身方向 | `submit_exploration`; 显式启用时可 `inspect_reference` |
| `integrator` | 已汇集的固定 V0 正文 | `submit_merges`, 仅提出既有 F/R/M 的合并 |

消息、todos、摘要及文件由各角色 SDK 图管理. 取证实例持有本角色测量记录、呈现标记和冻结字节. 子图只返回程序生成的状态/结果引用, 不合并自己的消息或 scratch 到父图.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 增加普通业务工具 | `_root_tools`、`_explorer_tools` 或 `_integrator_tools`; 业务校验放所属模块 |
| 调整角色方法 | 对应 skill; 工具 docstring 和模型提示也是运行输入 |
| 调整固定图像、上下文隔离 | `FixedInput`、`_child.project` 与 `ReferenceObservations.context` |
| 修改任务启动/结束条件 | 转到工作流的 `AnalysisSession`; 此处只接入准入回执 |
| 修改网络协议或输出参数 | 转到[模型适配](../../infrastructure/llm/README.md) |

## 关键约定

- 父模型填写的 task description 不改变冻结输入. SDK isolated 仍可能传递非消息状态, 因此项目显式投影输入和输出.
- 主图只准入本批槽位; 子图过滤并拒绝继续委派. `subagents=[]` 不等于禁用默认 general-purpose.
- SDK scratch 文件不是权威报告. 只有提交工具通过[五库规则](../../domain/five_libraries/README.md)才接受产物.
- 取证在后续请求准备时核对记录和真实图像并登记呈现; 这不证明服务端收到或模型理解. 测量锁保护同角色 ID、缓存和额度.
- 调用限制作用于各图普通模型节点, 不等于子图与摘要请求合计预算. 原生模型重试只包围请求, 不重派业务任务.

## 验证入口

[原生 task 与隔离](../../../../tests/unit_tests/agents/test_native_analysis.py)、[取证与交付](../../../../tests/unit_tests/agents/test_reference_observations.py). 按改动范围执行[开发检查](../../../../docs/development.md), 文档维护遵循[同步约定](../../../../AGENTS.md#文档同步).
