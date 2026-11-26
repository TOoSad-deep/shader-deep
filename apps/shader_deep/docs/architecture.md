# Shader Deep 当前架构

本应用提供观察、规划、生成和独立视觉评审入口。观察 Agent 交付整图 E/F/R，规划 Agent 基于固定观察组织 M/S，调用方选择 S 后进入生成与渲染。评审 Agent 按固定任务标准比较指定参考与渲染 PNG，发布可回读报告。旧完整五库报告沿用既有生成入口。本页保存跨模块关系和阅读导航，模块实现细节放在源码旁的 README。

运行方式见[应用 README](../README.md), 改动流程与检查见[开发指南](development.md), 取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md).

## 模块导航

| 你要理解或修改的内容 | 首先阅读 | 与其他模块的关系 |
| --- | --- | --- |
| 单观察 Agent、工具、skill 与上下文 | [分析角色](../src/shader_deep/agents/analysis/README.md) | 使用 SDK 图与工作流提供的业务能力 |
| 输入固定、接受快照、终态与交付 | [分析工作流](../src/shader_deep/workflows/analysis/README.md) | 连接角色、领域校验与文件存储 |
| E/F/R 观察、主归属与关系引用 | [观察合同](../src/shader_deep/domain/scene_observations.py) | 纯领域规则, 不依赖框架或 IO |
| 单规划 Agent、草稿工具与渐进式 skill | [规划角色](../src/shader_deep/agents/planning/README.md) | 共用 SDK 草稿运行辅助，固定原图与完整观察每轮入模 |
| 独立评审、层级模板与标准绑定 | [评审角色](../src/shader_deep/agents/review/README.md)、[工作流](../src/shader_deep/workflows/review/README.md) | 消费固定双图与标准，发布默认 V1 六维报告或显式 V2 逐项报告 |
| 规划输入、预算、接受与发布 | [规划工作流](../src/shader_deep/workflows/planning/README.md) | 绑定观察版本、要求与实际生成约束 |
| 新 M/S、有效备选与组合候选 | [规划合同](../src/shader_deep/domain/scene_planning.py)、[规划包](../src/shader_deep/infrastructure/storage/planning_package.py) | 领域派生与文件发布分工，正式包为 scene_planning_v1 |
| 五库对象、引用和合并保义 | [五库领域](../src/shader_deep/domain/five_libraries/README.md) | 纯业务规则, 不依赖模型框架或 IO |
| 模型配置、输出预算、流式与原始参数 | [模型适配](../src/shader_deep/infrastructure/llm/README.md) | 向角色提供客户端与请求/响应适配 |
| 报告包、证据、生成输入捕获 | [文件存储](../src/shader_deep/infrastructure/storage/README.md) | 发布合法产物并准备消费者的固定材料 |
| 生成、单方案、两方案比较或整图组合 | [生成架构](generation-architecture.md) | 使用明确选定的输入, 复用生成角色与渲染器 |
| 图像测量或 WebGL2 | [测量入口](../src/shader_deep/imaging/measurements.py)、[渲染入口](../src/shader_deep/rendering/renderer.py) | 确定性图像操作, 不判断视觉方案是否正确 |
| CLI 或本地网页 | [CLI](../src/shader_deep/cli/)、[网页任务](../src/shader_deep/web/jobs.py)、[网页服务](../src/shader_deep/web/server.py) | 参数/交互入口复用现有工作流 |

Python 消费者从 [api.py](../src/shader_deep/api.py) 进入。`resources/skills/` 提供角色运行规则，由观察和规划角色说明路由到对应 skill。模块 README 提供开发者阅读入口。

## 跨模块流程

[PNG 转 Shader 交互流程图（2026-11-20 设计快照）](../../../.archify/workflow-png-shader-agents-artifacts-20261120-171119/agents-and-artifacts.html)展示旧版角色交接设想；2026-11-25 评审后确认的新一版整体流程优化设计（含整图主干+局部隔离试验、`uniforms` 受控数值优化器、跨制品诊断与两道人工门禁）见[整体流程优化设计方案](work-items/workflow-optimization-design-20261125.md)。运行入口及门禁见[整体优化工作流](../src/shader_deep/workflows/optimization_workflow/README.md)，本地验证和真实模型校准进度见[实施记录](work-items/workflow-optimization-implementation-20261125.md)。

分析:

```text
CLI / Python API
  → 工作流固定原图、要求并创建业务会话
  → 一个 Deep Agent 观察全图, 按需读取规则样例
  → 保存上下文、E 下的 F 与场景 R 为磁盘草稿
  → 同一 Agent 回读复查, edit_file 局部修订
  → 完整回读最新稿并 finish, 程序校验和冻结接受
  → 在模型链之前终止后续请求
  → 工作流独立核查发布, 存储层回读验证后交付观察包
```

模型图负责工具循环, 会话管理草稿版本、回读回执与接受终态, 领域层验证主归属和引用, DraftStore 提供共享的草稿文件访问. 图异常或中断仍核查已有接受快照; 发布失败不登记成功结果. 详见工作流说明.

规划阶段接续已发布观察。

```text
run_planning / shader-deep-plan
  → 捕获观察正文、manifest、原图、要求和生成约束
  → 一个规划 Agent 保存 M/S 草稿并局部修订
  → 回读完整最新版本后 finish，冻结接受并发布 scene_planning_v1
  → read_planning_package 校验整包，read_planning_sketch 解析明确的 S 与可选备选
  → run_generation_from_planning 固定生效材料并登记整图任务
  → 共用生成角色、RenderSession 和真实渲染器
```

观察内容身份覆盖 E/F/R、manifest 与原图。规划始终使用捕获版本，方法条件、上游观察 gaps 与 S.gaps 随选中方案进入生成材料。候选组合索引由默认及备选生效视图派生，保留来源。

生成:

```text
PNG 或显式选定的报告/规划方案
  → 工作流固定输入, 创建新的生成任务
  → 生成角色提交 GLSL, RenderSession 管理线程与渲染资源
  → 实际渲染并在后续请求回读预览
  → 选择本轮成功候选或记录受阻原因
  → 保存实际 GLSL、预览和运行索引
```

报告模式、比较及整图组合的入口和准备顺序见生成架构. 比较中的人工选择只更新比较索引; 整图组合从两个已选来源建立新任务, 来源旧候选不变成本轮候选.

新整图任务通过 ScenePlanningBinding 绑定规划身份与显式 S，CapturedPlanningInputs 贯通任务校验、生成上下文和 RenderSession。生成默认继承规划尺寸与时间，显式覆盖差异写入本轮执行材料。

本地网页通过子进程运行已有 CLI, 不另建业务执行器. HTTP 服务仅监听回环地址, 限制同源操作、上传和会话制品访问; 运行方式见应用 README, 原实施与验证可按需查[网页记录](work-items/local-web-ui.md).

独立评审通过 `run_review` / `shader-deep-review` 显式接收参考 PNG、渲染 PNG 和任务。工作流选择 feature/element/scene 模板，固定标准与图像。V1 由模型整份提交六维状态与差异证据；V2 由模型登记每个待答项，领域规则将答案与固定的覆盖缺口汇总成六维报告。调用方依据正式回读的报告组织后续任务。[评审实施记录](work-items/independent-visual-review-implementation-20261117.md)

## 状态与依赖

| 事实或状态 | 归属 |
| --- | --- |
| 分析固定输入、调用计数、草稿回读状态与接受终态 | 分析工作流的 `AnalysisSession` |
| 规划固定观察、生成约束、计数与接受终态 | 规划工作流的 `PlanningSession` |
| 评审固定标准、双图、调用计数与接受终态 | 评审工作流的 `ReviewSession` |
| 评审六维状态一致性与总体结论 | `visual_review` 领域规则 |
| 可编辑草稿字节、版本与修改前快照 | 文件存储的 JsonDraftStore，由观察和规划适配绑定领域诊断 |
| 模型消息、skill 文件与摘要 | 各观察或规划角色的 SDK 图 |
| 观察字段与归属/引用合法性 | scene_observations 领域规则 |
| M/S、有效备选与候选索引 | scene_planning 领域规则，旧五库规则继续服务历史完整包 |
| 图像、报告与运行索引的文件表达 | 文件存储; 文件存在不替代业务接受 |
| 生成候选、预算、预览呈现与终态 | 生成角色的 `RenderSession` |
| 人工比较选择 | 比较工作流与比较索引, 与子运行自检分开 |

`domain/` 保持无 IO 与框架依赖，`rendering/` 保持独立。观察与规划使用 SDK 工具循环及上下文能力，通过 CompositeBackend 将各自草稿接入受控文件后端。[共享草稿运行辅助](../src/shader_deep/agents/draft_runtime.py)负责有序工具批次、实际回读回执和执行准入。应用管理固定输入、冻结接受与正式交付。

## 当前说明与历史记录

- [独立视觉评审设计](work-items/independent-visual-review-design-20261117.md)定义统一合同、三种层级模板与任务标准绑定, [评审指标准则](visual-review-rubric.md)定义视觉判断, [实施方案](work-items/independent-visual-review-implementation-20261117.md)维护开发与验证证据.
- 模块 README 说明当前职责和修改入口。新五库语义见[轻量五库业务设计](work-items/five-library-planning-contract-20261111.md)，历史完整包见[旧数据设计](analysis-five-libraries.md)。实现状态通过源码与对应验证记录核对。
- 核心公开分析/生成入口及合法五库读取保持. 旧四库执行、固定五库执行器和回放已删除; 保存着历史文件不代表支持续跑或自动转换.
- 查为何采用原生链或删除旧执行时, 再读[原生切换](work-items/deep-agents-native-plan-20261028.md)与[清理记录](work-items/analysis-legacy-cleanup-20261028.md). 当前改动的验证结果写入对应任务记录, 不在本页累计测试数字.
- completed、文件校验通过与渲染成功均不等于用户视觉验收. 文档维护遵循[同步约定](../AGENTS.md#文档同步).
