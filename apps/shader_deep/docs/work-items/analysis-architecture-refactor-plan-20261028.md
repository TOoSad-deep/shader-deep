# Shader Deep 分析模块架构整理实施计划

> 历史记录: 下文保留当时的实现、命令与验证结果. 旧分析执行和回放已退出当前包, 最新范围见[清理记录](analysis-legacy-cleanup-20261028.md).

- 日期: 2026-10-28, Asia/Shanghai.
- 状态: 2026-10-28 已按本计划完成代码整理与工程验收, 保留工作区既有功能修改; 尚未提交或推送. 本文前部为实施约束, 实际落点与验证见第 11 节.
- 范围: Shader Deep 分析模块及直接相关的兼容入口、实验调用方、测试和文档. 不修改上游 SDK.
- 起点: 编写时 HEAD 为 `30d6d5fb`, 工作区存在未提交和未跟踪的功能改动; 该提交本身不包含本计划依据的全部实现.
- 方法: 延续本次审查使用的 improve-codebase-architecture、codebase-design、deep-agents-core、deep-agents-orchestration 和 domain-modeling. 先集中职责、缩小调用方需要理解的 interface, 再迁移目录; 不按文件行数机械拆分.

## 1. 目标与完成定义

让维护者能直接找到当前分析角色, 在角色入口看清提示词、输入、工具和提交条件; 从工作流看清业务顺序; 从运行控制看清预算、权限、恢复和状态归属.

完成后应满足:

1. 当前编排、探索、整合、恢复角色有唯一的规范入口, 不与旧四库实现混名.
2. 主角色不再借用探索实现的私有运行函数; 当前角色不再依赖 `workflows` 的具体配置实现.
3. 当前分析的模型会话驱动只有一份共享实现; 请求/工具中间件、任务执行、业务动作和交付各有明确职责.
4. 角色不扫描 `TaskStore.snapshot()` 的内部任务表来推断预算或恢复归属; 累计配额与本地轮次有明确含义.
5. 当前行为、公开导入和函数签名、模型可见工具契约、磁盘记录和报告读取保持兼容.
6. 能从干净构建出的 wheel 导入新旧入口, 加载实际使用的 YAML 与 skill; 默认和兼容流程分别验证.

目录数量、文件数量和总代码行数不是验收目标. 不引入只有一个实际用途的注册系统、通用 AgentFactory 或包揽全部状态的对象.

## 2. 依据与当前约束

### 2.1 源码证据入口

| 已确认问题 | 当前证据 | 本计划处理位置 |
| --- | --- | --- |
| 当前角色集中在五库文件, 旧角色仍占用直觉名称 | 五库角色（历史路径 `src/shader_deep/agents/five_analysis/agent.py`, 已删除）、旧探索角色（历史路径 `src/shader_deep/agents/exploration/agent.py`, 已删除）、旧路径转发（历史路径 `src/shader_deep/analysis/exploration.py`, 已删除） | 阶段 1、4 |
| 主角色导入其他角色的私有函数, 角色反向依赖工作流配置 | 主角色（历史路径 `src/shader_deep/agents/main/agent.py`, 已删除）、[配置解析](../../src/shader_deep/workflows/configuration.py) | 阶段 1、2 |
| 中间件兼任驱动, 主角色重复实现 invoke 循环 | 公共运行器（历史路径 `src/shader_deep/runtime/runner.py`, 已删除）、主循环（历史路径 `src/shader_deep/agents/main/loop.py`, 已删除） | 阶段 2 |
| 任务、重派、并发、业务动作和交付集中 | 五库工作流（历史路径 `src/shader_deep/workflows/five_analysis.py`, 已删除） | 阶段 3 |
| 配额依赖任务 ID 命名, 状态读取暴露内部字典 | 五库角色的 `_charge_model`、TaskStore（历史路径 `src/shader_deep/runtime/task_store/store.py`, 已删除） | 阶段 2、3 |
| 分步实验调用默认角色的私有函数 | 实验运行器（历史路径 `src/shader_deep/experiments/exploration_skill/runner.py`, 已删除）、实验工具（历史路径 `src/shader_deep/experiments/exploration_skill/tools.py`, 已删除） | 阶段 1、2、5 |

设计依据为 [ADR 0001](../decisions/0001-agent-oriented-layout.md)、[当前架构](../architecture.md)、[分析工作流设计](../analysis-architecture-design.md)和[五库数据契约](../analysis-five-libraries.md). 本计划细化已有角色导向结构, 不将目录建议提前写成已接受的新 ADR.

### 2.2 实际依赖版本

2026-10-28 从应用 `.venv` 查询: Deep Agents 0.7.13、LangChain 1.4.0、langchain-core 1.6.2、LangGraph 1.2.11、langchain-openai 1.6.0. Deep Agents 实际加载本仓库 `libs/deepagents/deepagents` 源码.

实施基线同时记录安装版本、应用锁文件、SDK 源码路径及其 Git 差异. 可编辑 SDK 不能仅凭版本号确定实现; 不套用最新在线示例, 不在本轮升级依赖或改模型配置.

已核对的框架约束:

- `create_deep_agent` 按 middleware 名称替换默认同名实例, 不是无条件叠加.
- 当前业务探索由宿主线程池派发; SDK `task` 被应用权限约束屏蔽.
- SDK 的 `isolated` 和实验性 `fork` 不等价. `fork` 继承父会话, 不符合本轮独立观察要求.
- 当前会话通过显式 messages 延续, 未使用 checkpointer; `TaskStore` 恢复不代表恢复完整模型会话.
- 模型可见工具过滤与实际工具执行检查必须同时保留.

### 2.3 与正在进行的功能工作的衔接

工作区已有[分析优化](analysis-optimization-requirements-20261027.md)和[分步探索实验](exploration-skill-20261026.md)相关改动, 包括目标框、观察工具、证据发布、草稿与历史处理. 它们作为实施起点的现状核对, 不能按前一日较早的源码删除或覆盖.

阶段 0 固定实际输入基线后再迁移. 若同一文件仍在被其他工作修改, 先明确编辑归属并完成该文件交接; 无冲突的清单和测试准备可继续. 不使用全仓 stash、reset 或清理未跟踪文件来获得表面干净状态. 不读取或打包 `.env`、`1.txt`、真实运行制品等无关材料.

## 3. 本轮保持的行为与排除项

### 3.1 必须保留

| 类别 | 不变量 |
| --- | --- |
| 业务调度 | 默认一批两个或三个方向, 最多三个; 目标、方向和已冻结目标框不因恢复改变; 不追加探索批次 |
| 角色职责 | 主角色定位与编排; 探索独立产出 F/R/M/S; 整合只提出 F/R/M 合并; 草图由程序汇集并保义更新引用 |
| 上下文 | 不引入共同观察初稿、主会话全文或兄弟报告; 原图字节固定; 保留当前可选裁剪与取证开关的语义 |
| 材料准入 | 已登记、工具已返回、材料进入请求、提交被接受分别记录; 图片须实际进入多模态请求 |
| 权限 | 请求时权限快照、执行白名单、终态后拒绝动作、单会话工具顺序执行保持有效 |
| 预算与重试 | 调用/修复额度跨任务重派保留; 网络尝试单独计数; 不重放已执行工具; 无进展与有限恢复规则保持 |
| 状态与发布 | Attempt 写权、运行代次、父任务状态和封存检查不变; 同一权威提交记录发布结果、终态和回执 |
| 结果 | V0/V1 不可变; 整合失败保留 V0 和缺口; 失败任务不删除; 无合法基线不得伪造成功报告 |
| 对外契约 | Python/CLI 入口、函数身份及签名、工具名和 schema、配置键、输出和退出码保持兼容 |
| 文件包 | 既有报告和可选证据能继续读取、移动和交接生成; `run.json` 继续是派生投影 |

### 3.2 本轮不做

- 不改成 SDK `task` 调度, 不切换到 `create_agent`, 不增加 checkpointer 或自动模型摘要.
- 不更改五库字段、工具参数语义、提示词内容或默认观察工具策略.
- 不更改 `commit.json` 的存储版本、既有任务 ID、制品键、报告 schema 或默认恢复行为.
- 不删除仍需兼容的执行协议、历史数据或公开导入; 不重构生成与渲染业务.
- 不自动运行真实付费模型评估, 不把结构整理结果解释为视觉质量或 token 成本改善.

若实现必须改变以上任一项, 将该行为变更从结构迁移中拆出, 记录理由、调用方影响和独立验收, 不静默混入.

## 4. 术语、职责和依赖方向

### 4.1 术语

| 术语 | 本计划含义 | 状态归属 |
| --- | --- | --- |
| 分析运行 Run | 固定原图和用户要求下的一轮业务执行 | 工作流装配, TaskStore 持久事实 |
| 角色 Role | 编排、探索、整合、恢复的能力与职责 | 角色定义; 不从业务任务 ID 散落推断 |
| 方向 Direction | 同一探索角色的任务侧重点 | 冻结的 TargetPlan |
| 逻辑任务 Task | 固定输入、身份和累计额度, 可有多个 Attempt | TaskStore |
| 执行尝试 Attempt | 一次执行授权及代次; 失效后不可写入 | TaskStore |
| 模型会话 Session | 一次角色运行的消息历史与临时材料 | 该角色执行实例 |
| 逻辑调用 Model call | 一次应用层模型调用, 内部可能发生有限网络重试 | 持久配额和会话诊断分别记录 |
| 网络尝试 Request attempt | 一次实际传输尝试 | 传输诊断 |
| 库快照 / 草稿修订 / 交付投影 | 业务报告、过程草稿和最终选择/状态的不同产物 | 同一权威提交记录下的分类操作 |

术语整理首先写入现有架构文档, 不创建重复的进度系统. 是否增设纯术语 `CONTEXT.md` 取决于后续维护需要, 不是本轮交付前置条件.

### 4.2 目标职责

| module | 应承担 | 不应承担 |
| --- | --- | --- |
| 角色定义 | 系统提示词、选材、模型可见工具和提交校验接线 | 扫描全部任务推算预算、发布最终文件包 |
| 分析会话装配 | 按角色构建实际 harness 和注入执行能力 | 决定新增方向、保存第二份任务状态 |
| 请求/工具中间件 | 权限、上下文注入、响应与提交反馈处理 | 并发派发任务或自行选择最终报告 |
| 会话驱动 | 调用图、延续消息、处理未有效提交和结束条件 | 解释五库或任务 ID 命名 |
| 任务执行 | Attempt 生命周期、并发、恢复决策调用和收集 | 生成特征/机制、维护模型消息副本 |
| 运行控制 | 配额扣除、活动授权、恢复身份和进展判断 | 导入具体角色或工作流 |
| 交付 | 汇集结果、选择版本、生成投影和发布包 | 重做视觉分析、另起隐藏探索 |
| TaskStore | 原子写入、不可变记录、回执、失效与封存检查 | 将模型自然语言当作完成事实 |

依赖约束:

1. 工作流装配角色和运行能力; 角色消费已解析的阶段选项及明确的宿主动作协议.
2. `runtime` 不导入 `agents`、`workflows`、`compatibility`; 需要角色行为时由上层注入.
3. `domain` 保持无 IO、无模型框架依赖; 提交契约与引用校验不为迁移改变语义.
4. 当前实现只依赖规范入口; 兼容实现可以使用当前公共能力, 当前实现不反向调用旧流程.
5. 对已有独立用途保留 seam. 只有转发、没有隐藏规则的函数不单独扩成 module.

## 5. 目标目录与迁移映射

以下为拟落点, 不是已经存在的目录. 路径相对于 `src/shader_deep/`. 只有实际承载职责时才创建文件, 不为目录对称性建立空模块.

```text
agents/
  analysis/
    __init__.py          # 保留旧 agents.analysis 的公开导出
    orchestrator.py      # 主角色、业务工具组装与专属请求策略
    exploration.py       # 探索输入、工具与提交入口
    integration.py       # 整合角色与正文读取能力
    recovery.py          # 恢复决策角色
    execution.py         # 分析角色共用的 harness 装配
    contracts.py         # 宿主动作协议; 不复制领域模型
    skill.py             # 编排 skill 发现、读取和呈现确认
    evidence.py          # 工作者证据能力与请求呈现
    prompts.py           # 原提示词集中迁移, 内容保持
workflows/
  analysis/
    __init__.py          # 保留 run_analysis / run_analysis_task 导出
    entry.py             # 原 workflows/analysis.py 的运行输入与输出装配
    pipeline.py          # 当前五库主流程及持续主角色执行
    actions.py           # 主角色的宿主业务动作
    tasks.py             # 任务派发、并发、重派和结果收集
    delivery.py          # V0/V1、异常交付、最终投影
domain/
  analysis.py            # TargetPlan / RecoveryDecision 等纯契约
  five_libraries/        # 保留五库领域规则
runtime/
  model_loop.py          # 运行已组装图的公共会话驱动
  middleware.py          # 原 AnalysisLoop 中通用请求/工具处理
  analysis_control.py    # 分析运行授权、共享配额和恢复上下文控制
  runner.py              # 原 AnalysisLoop 的兼容入口, 不复制循环
  options.py             # 最小运行协议及已解析角色选项
  task_store/            # 保留持久化实现, 增加聚合读取/操作而非外泄字典
compatibility/
  four_library/          # 旧四库工作流与角色的实际实现
  fixed_five_analysis/   # 非默认固定阶段五库流程, 仅在仍有调用时保留
experiments/
  exploration_skill/    # 保持显式实验, 改用声明的共享执行入口
```

已有 `runtime` 审计、传输和存储能力应优先复用; `RoleAudit` 可归入 runtime 的审计文件, 无需角色间互相导入. `LibraryReader` 先与整合角色归并; 若实验确有独立调用再保留单独文件.

### 5.1 符号迁移清单

| 当前归属 | 目标归属 / 处理 |
| --- | --- |
| `agents/main/agent.py`、`loop.py`、`skill.py` | `agents/analysis/orchestrator.py`、`skill.py`; 公共驱动进入 runtime |
| `agents/five_analysis/agent.py` 的各 `run_*` | 对应角色文件; `run_planning` 属于旧固定阶段链, 不作为当前第二个主角色 |
| `_run` 与 Deep Agent 构造参数 | `agents/analysis/execution.py`; 只保留一个当前角色装配入口 |
| `_charge_model`、`_active`、恢复父任务识别 | `runtime/analysis_control.py` 和 TaskStore 的聚合操作 |
| `_commit` | 提交执行归入运行控制; F/R/M/S 业务校验仍由领域函数负责 |
| `TargetPlan`、`RecoveryDecision` | `domain/analysis.py`, 类名及 schema 保持; 旧契约路径重导出 |
| `MainAgentActions` 协议 | `agents/analysis/contracts.py`; 实现仍由工作流持有 |
| `_execute_task`、`_wait_workers`、恢复决策安排 | `workflows/analysis/tasks.py` |
| `MainAnalysisActions` | `workflows/analysis/actions.py` |
| 基线恢复、版本选择、交付投影 | `workflows/analysis/delivery.py` |
| `_main_stages`、`execute_five_analysis` | `workflows/analysis/pipeline.py`; 原执行入口保留转发 |
| `workflows/options.py`、`configuration.py` | 先保留公开配置兼容; 向角色传解析后的运行选项, 清除角色对工作流的反向依赖 |
| 旧 outline/exploration/integration 和 coordinator | 阶段 4 整组迁移到四库兼容实现; 旧导入暂留薄转发 |
| 实验对 `_run`、`_commit`、提交工具的私有导入 | 同步改为共用的执行/提交能力; 实验方法与默认业务策略继续分开 |

### 5.2 文件与包同名冲突

`agents/analysis.py` 和 `workflows/analysis.py` 必须在各自迁移批次内替换为同名包, 不能让 `.py` 与同名目录同时留存后依赖导入优先级.

- 新包保留原模块的可导出函数, 包括既有函数身份和签名要求.
- `agents.analysis` 的兼容导出避免在包初始化时反向加载整个工作流; 必要时采用惰性导出并验证导入顺序.
- 不在 `__init__.py` 建立模型、线程、浏览器或运行目录.
- 对新旧入口分别做独立进程导入, 再在 wheel 解包环境验证, 防止可编辑安装掩盖遗漏.

## 6. 分阶段实施

各阶段为可独立验证和回退的变更批次. 用户随后已授权由 GPT-6.1 Sol 子 agents 实施; Git 发布不在本轮执行范围. 进入实施后, 已确定范围内的常规步骤不需要逐项重复确认.

### 阶段 0: 固定基线与调用清单

**工作:**

- 记录 Git HEAD、相关文件差异/指纹、安装与锁定版本、SDK 可编辑源码状态; 保留未跟踪功能文件.
- 建立默认、四库兼容、固定五库、导入转发、实验五类调用清单. 对 `_stages`、`run_planning` 核对生产、脚本和测试引用, 不因默认入口不用就直接删除.
- 列出迁移涉及的公开函数、类、工具 schema、提示词、资源路径和现有磁盘键.
- 跑相关离线行为测试与 `make repository_check`; 记录现有失败及完整日志, 不沿用此前 87 项通过作为今日基线.

**退出条件:** 输入版本可定位; 每个待迁移旧入口均有去向和实际调用方记录; 已知失败有变更前证据.

### 阶段 1: 当前角色入口与领域契约归位

**工作:**

- 按第 5 节建立当前分析角色入口, 迁移提示词、skill、证据能力与提交工具.
- 将纯任务契约移入 domain, 保留类对象重导出, 不复制两套模型.
- 引入共用角色装配入口, 把主角色与分步实验对其他角色私有函数的调用转为该入口.
- 宿主提前解析阶段选项; 保留旧 `outline_*` 等配置键及优先级, 内部用明确角色语义解释.
- 当前旧路径改为薄转发, 处理 `agents.analysis` 文件/包替换及潜在循环导入.

**验收:** 同一任务的模型可见提示词、工具 schema、输入图像和提交结果保持; 主角色、独立探索与分步实验仍可运行; 新旧公开导出保持身份与签名.

### 阶段 2: 统一会话驱动、中间件与运行控制

**工作:**

- 从 `AnalysisLoop.run` 和 `MainAgentLoop.run` 提取共同驱动, 接收已装配图和结束/继续策略; 主角色与 worker 的差异留在装配和请求策略.
- 把请求/工具拦截与驱动分开, 保留旧 `AnalysisLoop` interface, 旧调用方委托公共实现而不复制循环.
- 集中授权检查、恢复关系、共享配额规则. 在 TaskStore 的同步范围内提供必要的聚合检查与扣费, 不把“先读再写”的竞争暴露给角色.
- 保留存储 JSON 格式. 任务 ID 到角色的旧命名解释集中在一处兼容映射; 新角色定义不再到处使用字符串前缀判断.
- 明确本地响应轮次、持久调用扣费和网络尝试, 保留现有诊断字段的外部兼容映射. 扣费失败、请求取消和传输重试各有清楚的状态结果.
- 主角色 skill 目录发现、全文读取、实际呈现后开放业务工具的时序保持. 禁止同一响应中利用刚完成的读取/登记绕过权限快照.

**验收:** 一份公共驱动覆盖当前主角色和 worker; 并发恢复不超共享额度; 重派不清空累计额度; 未授权工具、终态后提交、失效 Attempt 均拒绝; 自动摘要、工具顺序及文件处理策略没有漂移.

### 阶段 3: 工作流分离与状态读取收口

**工作:**

- 将工作流分成入口、pipeline、actions、tasks、delivery, 同批完成 `workflows.analysis` 文件/包替换.
- 主流程只表达准备、持续主角色执行、异常结果与交付; 任务线程、重派和恢复决策安排归入 tasks.
- 将角色所需任务状态投影为最小只读视图; 将角色/预算/恢复信息查询收拢到运行控制与 TaskStore, 清除角色扫描全体任务表的做法.
- 库快照、草稿修订、证据快照和交付投影使用语义明确的发布/读取入口, 底层继续写入现有 `versions` 容器和同一提交记录.
- 将包发布与封存顺序保持在同一工作流交付链, 不移动为模型决定或异步无确认写入.

**验收:** 重开既有运行目录后可识别已接受任务、V0/V1 和准备中的交付; 中断/封存失败后不重复任务、不丢失可用报告; 目标框、证据和已发布包来自固定输入与权威记录.

### 阶段 4: 旧实现隔离与导入检查完善

**工作:**

- 将仍有用途的四库角色与 coordinator/legacy 工作流整组迁入兼容目录, 让默认角色目录只包含当前实现和必要导入转发.
- 对固定五库阶段链, 有明确用途则隔离到兼容实现; 无调用证据时列为后续删除候选, 本轮不据此擅自删除.
- 更新所有仓库内调用方到规范路径, 保留外部旧导入; 新规范实现禁止依赖旧转发路径.
- 更新 `scripts/check_repository.py` 的规则: 对明确列出的纯兼容导出文件允许指向 compatibility, 并验证这些文件只承担转发. 不对整个 agents/workflows 目录放宽规则.
- 先为新增规则增加“合法薄转发可通过、当前实现反向依赖旧流程被拒绝”的验证, 保持 domain/runtime/rendering 既有约束.

**验收:** 默认流程不经过兼容实现; 旧 Python 导入与旧回放仍能工作; 动态/相对/类型导入按现有检查能力覆盖; 没有相互转发和两套有效实现.

### 阶段 5: 文档、打包与整体回归

**工作:**

- 更新架构地图、分析工作流设计、开发阅读路线和必要模块 README; 历史记录保留日期和链接, 不批量改写成当前实现说明.
- 更新应用 AGENTS 的当前代码导航; 如果实际依赖约束有变化, 补充决策理由并同步检查, 不将旧 ADR 静默改成另一套决定.
- 执行整体检查、CLI 帮助、wheel 构建与分发检查. 资源路径优先不动; 如移动必须同步 package-data 并从 wheel 验证.
- 以一条默认分析链和一个实验入口做源码走读, 记录角色输入/工具/结束条件、调度/交付与状态所有者是否能各自在单一入口找到.
- 将实施范围、完整验证记录和剩余问题追加到本文的实施记录, 不另开一份重复状态台账.

**退出条件:** 第 9 节清单全部通过或准确列明阻塞; 没有将测试通过写成真实模型效果或用户视觉验收.

## 7. 验证矩阵与命令

命令均在 `apps/shader_deep/` 执行, 使用现有环境和标准库 unittest. 不加载真实 `.env`, 不开启新的外部追踪配置.

| 变更面 | 优先复用的测试 | 重点保护 |
| --- | --- | --- |
| 入口迁移 | `tests.unit_tests.compatibility.test_public_api` | 函数身份、签名、旧路径导出 |
| 主角色与 harness | `tests.unit_tests.agents.test_main_agent` | skill 全文呈现、同批权限、工具串行、大结果回读 |
| 任务与配额 | `tests.unit_tests.runtime.test_task_store`、`tests.unit_tests.workflows.test_five_analysis` | 持久调用/修复、并发恢复、迟到提交、失败与中断 |
| 工作流动作/交付 | `tests.unit_tests.workflows.test_main_actions`、`tests.unit_tests.workflows.test_five_public_api` | 冻结批次、V0 兜底、包发布与封存 |
| 目标框与证据 | `tests.unit_tests.workflows.test_analysis_focus_recovery`、`tests.unit_tests.agents.test_analysis_evidence_review` | 重派框不漂移、证据字节固定、移动后可读 |
| 实验与上下文 | `tests.unit_tests.agents.test_exploration_skill`、`test_analysis_optimization`、`test_exploration_read_progress` | 草稿修订、撤回、真实请求材料、有效读取进展 |
| 传输与工具契约 | `tests.unit_tests.runtime.test_openrouter_transport`、`test_analysis_tool_schema` | 工具 schema、本地校验、推理块及工具消息配对 |

阶段 0 的核心行为基线示例:

```sh
uv run --no-sync python -W error -m unittest \
  tests.unit_tests.runtime.test_task_store \
  tests.unit_tests.workflows.test_five_analysis \
  tests.unit_tests.workflows.test_main_actions \
  tests.unit_tests.agents.test_main_agent \
  tests.unit_tests.compatibility.test_public_api -q
make repository_check
```

各阶段先运行对应测试. 涉及业务执行接线、工具/中间件或配置契约的变更, 按项目要求执行 `make test` 和 `make lint`; 涉及目录和导入的变更执行 `make repository_check`. 收尾统一运行:

```sh
make check
uv run --no-sync python -m shader_deep.cli.analysis --help
uv run --no-sync python -m shader_deep.cli.replay --help
uv build --out-dir /private/tmp/shader-deep-architecture-dist
# 用实际构建出的 wheel 路径替换下一行参数.
uv run --no-sync python scripts/check_distribution.py /private/tmp/shader-deep-architecture-dist/shader_deep_app-0.1.0-py3-none-any.whl
```

若实际修改图像请求/证据交接或生成预览链, 补 `make integration_test`; 单纯迁移 import 不自动触发真实模型评估. 锁文件有漂移时记录为独立问题, 不通过自动升级使重构检查变绿.

新增测试仅保护现有覆盖不足的可观察行为, 例如新旧入口导入顺序、拒绝非法依赖、额度扣除失败后不能发送请求. 不断言目录字符串、私有调用顺序或文件行数来证明重构成功. 原测试补丁路径随实际依赖位置迁移, 不删除业务断言掩盖回归.

## 8. 风险、暂停条件与回退

| 风险 | 处理 |
| --- | --- |
| 与当前功能修改互相覆盖 | 阶段 0 保存范围与指纹, 修改同一文件前核对差异; 发现新变更先合并意图, 不覆盖 |
| 文件转包引入循环导入 | 同一批完成替换与导出; 独立进程交换导入顺序, wheel 中再次验证 |
| runtime 变成新的业务集中点 | runtime 不识别具体角色实现、不派发探索; 宿主注入行为, 领域合同保持纯数据 |
| 共享预算或恢复条件变化 | 扣费与权限在原子操作内验证; 中断、并发、重派行为先有测试再迁移 |
| middleware 顺序或过滤变化 | 捕获真实发送给假模型的提示词/工具/图像及执行结果; 不只比较构造参数 |
| 兼容文件令静态检查误报 | 精确列出薄转发并检查内容; 禁止用目录级忽略掩盖反向依赖 |
| 已有磁盘记录失效 | 维持 schema/键/ID; 用迁移前合法记录验证恢复, 不在结构整理中引入数据迁移 |

每阶段保留可单独审查的差异与验证记录. 已授权提交时按阶段提交, 不混入既有无关变更. 若回归无法定位, 回退本阶段明确的文件差异到实施前记录, 保留其他任务变更; 不使用仓库级破坏性重置.

回退前结束本阶段测试/工作者并确认无在途提交. 不删除或覆盖失败运行目录、冻结报告或审计日志, 不用旧源码继续写入正在执行的同一运行.

出现必须修改公开签名、磁盘格式、默认调度或模型可见协议的需求时, 停止该部分迁移并拆出行为变更设计; 不阻塞其余独立且已明确的整理工作.

## 9. 最终验收清单

- [x] 当前四类角色有唯一规范入口, 默认链不导入旧角色实际实现.
- [x] 角色入口可直接找到输入、提示词、工具和提交条件; 不追另一角色私有函数.
- [x] 主角色、默认探索和分步实验共用运行能力, 当前分析只有一份会话驱动.
- [x] 当前角色不直接依赖 workflows 配置或扫描 TaskStore 全表.
- [x] 逻辑任务、Attempt、会话、模型调用和网络尝试有明确语义与所有者.
- [x] 旧任务 ID/存储键保持, 任务权威状态没有第二份可写副本.
- [x] 单批限制、上下文隔离、工具双重权限、恢复预算、不可变版本与部分交付通过回归.
- [x] 可选观察工具、目标框、证据与分步实验的现有行为保留.
- [x] 新旧公开入口、旧协议回放、构建包资源通过检查.
- [x] 文档表达实际实现状态, 失败与未覆盖范围可追溯.

## 10. 后续独立评估: 框架执行 adapter

2026-10-28 已开展[框架替换首轮评估](framework-evaluation-20261028.md), 包含离线行为对照与有上限的真实模型试验. 当前结论保持默认实现, 受限 worker 的局部精简继续作为候选; 不把首轮受记录器和服务故障影响的数据解释为替换成功.

本轮不以“使用了 SDK task”作为完成条件. 结构整理完成后, 再比较保持受限 Deep Agent、狭窄 worker 使用 `create_agent`、SDK `SubAgent/CompiledSubAgent` 三种方案.

比较必须使用同一业务任务合同, 检查固定图像材料、单批规则、工具白名单、独立历史、回执、预算和中断恢复; 明确 SDK isolated 模式可能传递的非消息状态. 无行为等价证据时保留现有宿主派发, 不因代码更短直接替换.

## 11. 实施记录

| 日期 | 阶段 | 修改与验证 | 状态 |
| --- | --- | --- | --- |
| 2026-10-28 | 计划编写 | 复核当前目录、依赖版本、公开导出、兼容检查与实验调用方; 新增本计划. 使用应用 `.venv/bin/python scripts/check_repository.py` 检查通过, 0 findings; 计划结构、所列测试/构建入口及 `git diff --check` 检查通过 | 未开始实施 |

计划编写阶段仅修改文档, 当时未运行业务单元测试、lint、浏览器集成或真实模型. 上述静态检查不代表本计划中的重构已经实现或通过行为验收.

实施者在此追加每阶段实际路径、源码基线、命令、结果、完整失败日志位置和未覆盖范围. 设计完成、实现完成、工程验证、真实模型评估、提交/推送/合并分别记录.

### 2026-10-28 实施与验收

用户授权后, 三个 GPT-6.1 Sol 子 agents 分别负责角色/实验、运行控制、工作流/旧协议迁移; 一个独立 Sol 子 agent 对照实施前快照做只读审查. 主 agent 整合检查规则、兼容转发、文档、构建和最终验收. 未提交或推送代码.

| 阶段 | 实际交付 |
| --- | --- |
| 0 | 保存应用源码、测试和文档基线至临时目录; 当前相关 90 项行为测试通过 |
| 1 | `agents/analysis/` 成为当前角色入口; `domain/analysis.py` 保持 TargetPlan/RecoveryDecision 类身份; 实验改用正式执行/提交入口 |
| 2 | `assemble_role` 为当前主角色与 worker 的统一图装配, `runtime/model_loop.py` 为公共消息驱动; middleware、五库进展和授权/配额各自归位 |
| 3 | `workflows/analysis/` 分为 entry、pipeline、actions、tasks、delivery 和返回契约; TaskStore 新增只读 TaskView 与锁内共享预算扣费, 原 JSON 格式和任务 ID 不变 |
| 4 | 旧四库角色和完整工作流移至 `compatibility/four_library/`, 旧固定五库链移至 `compatibility/fixed_five_analysis/`; 精确薄转发白名单拒绝藏入实现及当前代码回到旧入口 |
| 5 | 更新实际架构、开发路线、AGENTS 和兼容说明; 全套检查及 wheel 资源/导入/7 个 CLI 帮助检查通过 |

实际目录与草案的局部差异: 主角色请求权限策略单独放在 `orchestrator_loop.py`, 整合正文读取仍在 `views.py`, 审计保留 `agents/analysis/audit.py`; 它们分别管理请求权限快照、正文呈现和角色记录, 没有新增执行循环. `runtime/five_analysis_loop.py` 单独保留进展与扣费时机. `runtime/runner.py` 为旧使用者保留构造能力, 其消息驱动委托同一实现.

角色配置经宿主解析为 ResolvedRoleOptions; 规范入口兼容直接传入原选项, 使用幂等阶段解析. 旧 `outline_*` 等配置键和来源优先级保留. 角色不再导入工作流具体配置实现; `agents.analysis.__init__` 的惰性导出是保持旧运行入口的显式例外.

新旧模型循环中的消息延续、纯文本提醒、长度截断提醒、事件和工具串行规则按原行为保留. 库快照和交付投影的选择/发布逻辑集中到 delivery 与任务整合入口; 没有迁移磁盘记录或新建第二份状态.

### 验证结果与已修复问题

[机器可读验证摘要及输入指纹](evidence/analysis-architecture-refactor-20261028.json)记录绑定的代码输入和完整日志位置. 完整日志位于 `/private/tmp/shader-architecture-implementation-20261028/`, 不将临时日志路径当成已进入 Git 的附件.

- `make check` 最终通过: 600 项无网络单元测试, repository_check、Ruff、格式和 ty 全部通过. 定向测试与全套测试有重叠, 不累加为独立覆盖数量.
- 只新增 3 项必要行为测试: 并发共享额度及重开后的累计计数、恢复父任务与错误反馈视图、精确转发许可不能掩盖执行实现或反向依赖. 其余复用现有断言, 仅将 mock 改到真实依赖位置.
- 首次全套检查的 15 个错误均为按需兼容测试仍 patch 旧 legacy_analysis 模块; 改为实际实现位置后该模块 15 项通过, 随后全套通过. 保留首次失败日志.
- 旧回放迁移后指纹根目录的相对层级发生变化, 定向测试发现后修复为包根目录, 回放相关用例通过.
- 整合时修复 3 处导入排序和固定五库工作流的配置类型导入错误; 没有扩大忽略规则.
- 构建初次因临时缓存无 setuptools/wheel 且沙箱 DNS 不可用失败; 获得执行权限后只下载项目已声明的构建依赖到临时缓存, 未更改依赖约束或应用锁文件. 最终离线构建 sdist/wheel 成功.
- wheel 解包验证通过: 当前入口不加载旧执行实现, 新旧导出身份一致, YAML/编排与探索 skill/WebGL2/网页资源完整, 7 个 CLI 模块可显示帮助.
- 独立只读审查对比实施前函数正文、阶段预算优先级、锁内扣费与回执路径, 未发现已确认行为回归. 此结论受源码和已运行测试范围限制.

本轮未改渲染器、图像取证算法或请求选材语义, 未追加浏览器集成或真实模型调用. 未验证视觉质量或成本收益; SDK 派发替换仍为第 10 节后续评估项.
