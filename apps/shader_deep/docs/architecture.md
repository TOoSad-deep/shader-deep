# Shader Deep 当前架构

本应用提供两条独立流程: 从参考图分析并交付五库报告, 或生成、渲染和选择 Shader. 分析到生成由用户显式选择报告和草图, 不自动串接. 本页保存跨模块关系和阅读导航, 模块实现细节放在源码旁的 README.

运行方式见[应用 README](../README.md), 改动流程与检查见[开发指南](development.md), 取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md).

## 模块导航

| 你要理解或修改的内容 | 首先阅读 | 与其他模块的关系 |
| --- | --- | --- |
| 主/子 Agent、业务工具、skill 与上下文 | [分析角色](../src/shader_deep/agents/analysis/README.md) | 使用 SDK 图与工作流提供的业务能力 |
| 输入固定、槽位状态、V0/V1 与交付 | [分析工作流](../src/shader_deep/workflows/analysis/README.md) | 连接角色、领域校验与文件存储 |
| 五库对象、引用和合并保义 | [五库领域](../src/shader_deep/domain/five_libraries/README.md) | 纯业务规则, 不依赖模型框架或 IO |
| 模型配置、输出预算、流式与原始参数 | [模型适配](../src/shader_deep/infrastructure/llm/README.md) | 向角色提供客户端与请求/响应适配 |
| 报告包、证据、生成输入捕获 | [文件存储](../src/shader_deep/infrastructure/storage/README.md) | 发布合法产物并准备消费者的固定材料 |
| 生成、单方案、两方案比较或整图组合 | [生成架构](generation-architecture.md) | 使用明确选定的输入, 复用生成角色与渲染器 |
| 图像测量或 WebGL2 | [测量入口](../src/shader_deep/imaging/measurements.py)、[渲染入口](../src/shader_deep/rendering/renderer.py) | 确定性图像操作, 不判断视觉方案是否正确 |
| CLI 或本地网页 | [CLI](../src/shader_deep/cli/)、[网页任务](../src/shader_deep/web/jobs.py)、[网页服务](../src/shader_deep/web/server.py) | 参数/交互入口复用现有工作流 |

Python 消费者从 [api.py](../src/shader_deep/api.py) 进入. `resources/skills/` 提供角色方法, 由分析角色说明路由到对应 skill; 它与给开发者阅读的模块 README 用途不同.

## 跨模块流程

分析:

```text
CLI / Python API
  → 工作流固定原图、要求并创建业务会话
  → 主 Deep Agent 登记元素、冻结单批方向
  → SDK task 启动独立探索, 提交工具调用领域校验
  → 业务会话汇集合法来源为 V0
  → SDK task 启动整合, 接受保义合并得到 V1
  → 工作流核查实际产物与缺口, 存储层发布报告包
```

模型图负责执行, 业务会话判断哪些输入与提交有效, 领域层判断引用及合并是否合法, 存储层负责可校验的文件表达. 失败也会进入最终核查; 是否有合法版本可交付由实际产物决定, 详见工作流说明.

生成:

```text
PNG 或显式选定的报告/草图
  → 工作流固定输入, 创建新的生成任务
  → 生成角色提交 GLSL, RenderSession 管理线程与渲染资源
  → 实际渲染并在后续请求回读预览
  → 选择本轮成功候选或记录受阻原因
  → 保存实际 GLSL、预览和运行索引
```

报告模式、比较及整图组合的入口和准备顺序见生成架构. 比较中的人工选择只更新比较索引; 整图组合从两个已选来源建立新任务, 来源旧候选不变成本轮候选.

本地网页通过子进程运行已有 CLI, 不另建业务执行器. HTTP 服务仅监听回环地址, 限制同源操作、上传和会话制品访问; 运行方式见应用 README, 原实施与验证可按需查[网页记录](work-items/local-web-ui.md).

## 状态与依赖

| 事实或状态 | 归属 |
| --- | --- |
| 分析计划、槽位、接受的报告与版本 | 分析工作流的 `AnalysisSession` |
| 模型消息、todos、scratch 与摘要 | 每个角色的 SDK 图 |
| 对象字段与引用合法性 | 五库领域规则 |
| 图像、报告与运行索引的文件表达 | 文件存储; 文件存在不替代业务接受 |
| 生成候选、预算、预览呈现与终态 | 生成角色的 `RenderSession` |
| 人工比较选择 | 比较工作流与比较索引, 与子运行自检分开 |

`domain/` 保持无 IO 与模型框架依赖; `rendering/` 保持独立渲染能力. 原生分析优先使用 SDK 的工具循环、task、skills 和上下文机制; 应用保留固定输入、准入、领域提交与交付. 新模块或自定义机制须有具体需求依据.

## 当前说明与历史记录

- 模块 README 说明当前职责和修改入口; 五库详细语义见[数据设计](analysis-five-libraries.md). 实现与验证有差异时以当前源码及实际证据核对, 不用旧计划替代当前合同.
- 核心公开分析/生成入口及合法五库读取保持. 旧四库执行、固定五库执行器和回放已删除; 保存着历史文件不代表支持续跑或自动转换.
- 查为何采用原生链或删除旧执行时, 再读[原生切换](work-items/deep-agents-native-plan-20261028.md)与[清理记录](work-items/analysis-legacy-cleanup-20261028.md). 当前改动的验证结果写入对应任务记录, 不在本页累计测试数字.
- completed、文件校验通过与渲染成功均不等于用户视觉验收. 文档维护遵循[同步约定](../AGENTS.md#文档同步).
