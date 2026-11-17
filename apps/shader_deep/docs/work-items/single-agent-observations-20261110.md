# 单 Agent 全图 E/F/R 观察实施方案

- 日期: 2026-11-10.
- 状态: 阶段 1–3 已实现并通过离线检查; 已获授权运行两轮五图校准, 五图均可合法交付, 语义粒度尚未全部通过. 阶段 4 保持未完成, 见文末实测记录.
- 分支: `TOoSad-deep/repo/single-agent-observations`.
- 准备保存点: `c911d3ca`; 原分支文档保存点为 `b385162f`. 当前改动已切换到单观察角色, 尚未提交 Git.
- 当前续接点: 复查粉色圆的 R 参与项、磨砂叠圆的 F/R 重复与小件群的布局误读; 按既定语义门槛继续校准, 不通过修改程序状态掩盖问题.

## 目标与完成边界

由一个原生 Deep Agents 观察角色分析整张参考图, 输出上下文、多元素 E、各元素的 F 和场景层 R. 程序负责固定原图、工具权限、结构校验、状态、文件发布和导航. 模型只维护一份语义提交.

本次跑通输入、观察、提交、修正、交付与回读. 多视角、M/S 规划、证据关联、跨运行自动记忆与 VISTA 生成回看不在首版范围. 旧完整报告及其生成消费者继续可用; 新观察包不能自动变成生成就绪包.

公开入口 `run_analysis` / `run_analysis_task` 保持现有参数位置与名称, `AnalysisOutcome` 保持现有字段. **默认分析的报告协议及旧配置可用范围会变化**, 实施时须在 README、CLI 帮助和迁移说明中明确告知. 新参数仅以带默认值的 keyword-only 参数加入.

新运行采用单一观察执行路径. 多角色调度不作为隐藏回退保留; 旧五库类型、校验和读取能力按实际消费者保留. 删除旧执行代码不等于删除旧报告支持.

## 阅读入口与当前阻点

- 开始编码前读[应用约定](../../AGENTS.md)、[分析角色](../../src/shader_deep/agents/analysis/README.md)、[工作流](../../src/shader_deep/workflows/analysis/README.md)、[文件存储](../../src/shader_deep/infrastructure/storage/README.md).
- 粒度判断依据[观察规范](../analysis-observation-guidelines.md)和[五图样例](../analysis-observation-examples.md), 本文不复制全部视觉规则.
- 修改配置时读 [options.py](../../src/shader_deep/workflows/options.py)、[configuration.py](../../src/shader_deep/workflows/configuration.py)、[模型参数解析](../../src/shader_deep/infrastructure/llm/options.py).
- 修改消费者时核对 [api.py](../../src/shader_deep/api.py)、[生成输入](../../src/shader_deep/infrastructure/storage/generation_inputs.py)、[网页任务](../../src/shader_deep/web/jobs.py).

现有 TargetPlan 冻结单目标, 探索输入只有该目标, 汇集时全部 F 归属唯一 E. F/R 无候选 M 会被要求填写 gaps. `_skill_files` 只预置角色 SKILL.md. 报告及公开结果按 five_libraries_v1 解释, 网页直接读取 sketches.json; 不能仅换提示词完成迁移.

## Review 结论的落实

| 问题 | 本方案的处理 | 必须验证 |
| --- | --- | --- |
| P1 提交与结束混用, 同批动作竞争 | 提交普通回执; 终态控制是否再请求模型; 终态工具单独调用 | 非法提交能修正, 接受后无新模型请求, 冲突批次不执行 |
| P2 文件限制阻断 SDK 回读 | 独立 StateBackend, 允许本轮 SDK 外置材料的只读访问 | 历史和大工具结果外置后可以读回 |
| P2 协议与配置迁移不完整 | 新读取入口、真实协议标识、旧配置迁移表 | API/CLI/网页与生成入口正确区分观察包 |
| P2 改号造成正文指代漂移 | 保留合法短 ID, 机器引用放明确字段 | 非连续 ID 保存与读取后不变 |

## 1. 观察提交合同

新增 `domain/scene_observations.py`, 与旧 Feature.mechanism_refs、Sketch 和探索合同解耦. 使用严格、不可变模型并拒绝未知字段; 接受时冻结嵌套内容.

| 对象 | 字段 | 约束 |
| --- | --- | --- |
| SceneObservationSubmission | context_notes、elements、relations、open_questions、gaps | 除 elements 外的列表可默认空; 全部可见观察为空时拒绝 |
| 提交元素 | id、name、region、features | name/region 为非空文字; 登记的 E 至少有一项 F |
| 提交特征 | id、description | 位于所属 E 下, description 非空 |
| 关系 | id、participants、description | participants 为 E/F ID 数组, 至少两个不同的合法参与项 |
| 未知项或缺口 | refs、description | refs 可空表示全图, 否则只引用本图 E/F/R |

ID 分别匹配 `E[1-9][0-9]*`、`F[1-9][0-9]*`、`R[1-9][0-9]*`. F 的 ID 在全场景唯一, 不在各 E 内重新计数. 编号允许跳号, 保存时不重新编号. participants 可混合引用 E/F, 允许同元素内的 F/F 关系; 不对共现对象自动补 R.

纯背景图允许 elements 为空, 但须有实际背景观察; 不强造主体. 有主体时 context_notes 可为空, R 也可为空. 程序只验证合同, 不用关键词假装证明观察正确.

机器关联只来自结构字段. description 用自然语言说明现象, 不沿用旧 `[[ID]]` 的强制解析与改写合同. 问题中的 refs 负责明确关联, 无需从正文猜引用.

示例只展示字段形式, 完整粒度以五图样例为准:

```json
{
  "context_notes": ["白色背景"],
  "elements": [{
    "id": "E1", "name": "圆角面板", "region": "画面中央",
    "features": [
      {"id": "F1", "description": "白色圆角矩形外形"},
      {"id": "F3", "description": "面板外侧有灰色柔影"}
    ]
  }],
  "relations": [],
  "open_questions": [],
  "gaps": []
}
```

程序将嵌套 F 摊平为保存用的观察集合, 派生 E.feature_ids. 每个 F 恰有一个主归属, 模型不填写第二份映射. 不执行语义合并、自动补写观察或候选机制.

## 2. Agent、上下文与记忆

在现有 agents/analysis/native.py 装配一个 create_deep_agent, 复用已有客户端、传输校验、网络恢复和固定原图. 工作流只 invoke 一次 SDK 图, 不增加外层反复提示或修复循环.

新角色直接由通用配置构造 ResolvedModelOptions, 再交给现有 configure_analysis_model. 不把 observer 传入当前只接受 outline/integration/worker 的 resolve_role_options, 也不借用 worker 阶段偷偷继承旧阶段预算. 不更换模型 ID 或 provider.

| 输入 | 装入方式 |
| --- | --- |
| 角色、观察边界、核心粒度与提交要求 | 固定指令, 从同一运行资源加载 |
| 用户要求、整图实际字节、原始尺寸 | 请求级固定输入, 每次实际主模型请求保持可见 |
| 剩余主模型预算与业务状态 | 程序生成简短上下文 |
| 消息与工具回执 | SDK 管理, 保留调用配对 |
| 详细规则和五图文字样例 | 显式预置到 StateBackend, 给出路径与读取触发条件 |

新资源建议为 resources/skills/scene-observer/SKILL.md 和 examples.md. 实施时将运行提示与样例原文迁至资源目录; 原规范文档保留解释并链接运行规则, 样例文档改为导航, 避免两份正文并行维护. 必需核心段直接注入, 不只提供 skill 名称期待自行发现.

原图在入口捕获一次, 模型输入和报告哈希复用固定字节, 不跟随源文件变化. 检查首轮和修正轮实际请求各含一份原图, 不向持久历史反复追加副本. 样例图片不默认附送; 首版不挂载裁剪与区域统计工具.

本轮工作记忆由 SDK 消息、摘要和文件状态管理, 应用不复制第二份模型历史. 跨运行口径通过人工确认的规则与样例版本积累; 猜测不自动写入长期规则, 历次报告不自动载入. 文件快照不承诺跨进程恢复完整模型会话.

## 3. 工具与执行控制

| 工具 | 行为 |
| --- | --- |
| submit_scene_observations(report) | 合同错误返回字段路径与原因并保持 running; 合法则冻结快照并记录 accepted |
| stop_analysis(reason) | 非空受阻原因; 记录 stopped, 不发布成功包 |
| SDK read_file | 读取本角色规则、样例及本轮 SDK 外置文件 |

模型可见工具与执行白名单同时生效. 规则只读, 不映射宿主机任意目录. 每次运行使用独立 StateBackend; 允许 SDK 实际 artifacts 路径下的 conversation_history、large_tool_results 等回读, 不假定永远位于根目录. 路径先规范化并验证所属范围.

subagents=[] 不足以禁用默认 general-purpose. 使用当前 SDK 配置或应用中间件关闭可达委派, 并在执行端拒绝猜测的 task、写入和 shell 调用; 不为此修改 SDK.

终态工具必须是该响应中的唯一工具调用. 两次提交、提交与停止、读取与终态动作的批次在执行前整体拒绝, 每个调用返回配对错误, 业务状态不变. 执行准入从本轮 AIMessage 的完整 tool_calls 判断批次, 不仅检查当前单次调用的工具名; 冲突批次中的 read_file 也不进入 handler. 普通只读工具按 SDK 正常执行. 同步与异步采用同一规则.

提交工具不设无条件 return_direct=True. Schema 和业务校验错误均成为可修正的工具回执; 传输截断不猜测补全. 非预期 IO/运行异常进入失败诊断, 不伪装成模型字段错误.

接受或停止后, 使用 SDK 的 before_model / abefore_model 跳转结束钩子, 在进入整个模型包装链之前结束, 不是仅在内层 wrap_model_call 返回替代响应. 该检查先于调用计数、网络重试与 SDK 摘要, 保证终态后连摘要模型请求也不再发生. 工具已写入的配对回执保留, 工作流从会话读取结果, 不依赖最后一条自然语言回复. 同步与异步钩子均须覆盖.

接受前先完成领域校验和不可变候选构造. 接受快照写入成功后才提交 accepted 状态并回成功回执; 写入失败进入执行失败, 不假装提交可修正或发布成功. 终态检查与内存状态提交受同一会话锁保护, IO 在锁外, IO 后再次确认终态未改变. 若终态已改变, 已写候选不能登记为接受结果. 终态后的业务写入均被拒绝.

## 4. 状态、预算与交付

会话只保留 running 到 accepted/stopped 的推进, 加上失败诊断. 不再保留探索槽位、冻结方向、V0/V1 与整合选择. 公开交付状态由工作流根据快照和发布结果生成.

| 情况 | status | report_dir |
| --- | --- | --- |
| 合法提交, 未声明必要观察 gaps, 发布成功 | completed | 已发布目录 |
| 合法提交, 声明必要观察 gaps, 发布成功 | partial | 已发布目录 |
| 无合法提交、主动停止或发布失败 | failed | 无已发布目录时为空 |

输入和迁移错误在启动前抛出, 沿用公开错误语义. 运行异常或预算结束时先核查快照: 有快照则尝试交付, 无快照则 failed. 中断沿用“核查并尝试保存已有产物后继续传播”. 发布失败保留接受快照与错误, 不把失败描述为已交付.

发布属于独立的 try/except 核查步骤, 不能把 on_delivery 回调留在统一异常处理之外. 写包或正式回读失败时, 记录 failed、publish_error、接受快照位置和空 report_dir, 再尝试保存 result.json / run.json; 不登记成功的 ResultRecord. 若连失败索引也因磁盘错误无法保存, 向调用方传播原始 IO 错误并给出已有运行路径, 不承诺索引一定存在. 原中断优先继续传播, 交付/诊断失败作为附加诊断保留.

open_questions 表达无法确认的观察; 影响必要覆盖时同时记录 gaps. 不要求讨论本阶段之外的机制未知. completed 仅表示观察合法交付且未声明必要缺口, 不证明无遗漏或视觉正确.

新增 max_model_calls=8 作为可调整的初始建议, 要求正整数. 读材料后的判断、错误修正与纯文本回复都受该上限约束. SDK 摘要与网络重试单独记录, 8 不代表所有 HTTP 请求总数. 沿用有限重试和超时; 业务校验失败不触发网络重发.

普通文本结束且未提交时失败, 不在图外补问. 合法接受的快照不能被随后的模型限额覆盖成无提交. 报告在图结束后发布: 同级暂存目录写齐、正式读取器复验后改名, 不覆盖已发布目录. 单文件快照不等于多文件事务或崩溃自动恢复.

## 5. 观察包与公开读取

新协议为 scene_observations_v1, 不要求单个 target_element_id:

```text
run-*/
  run.json                 # 实际协议、配置、规则版本和调用统计
  result.json              # 公开终态、原因、报告位置
  results/                 # 接受快照, 复用已有能力
  report/
    manifest.json          # 协议、status、reference、context_notes、问题与缺口
    reference.png          # 固定原图
    elements.json          # E 与程序派生的 feature_ids
    features.json          # F 的 id、description
    relations.json         # R 的 id、participants、description
    README.md              # 同一快照派生的全图树状分析
```

日志沿用现有基础设施. 包内合同如下, 与提交合同通过程序投影连接:

| 文件 | 保存结构 |
| --- | --- |
| manifest.json | schema_version 固定 scene_observations_v1; status 仅 completed/partial; reference 为 path=reference.png 和 sha256; context_notes、open_questions、gaps 默认空 |
| elements.json | 元素数组, 每项 id、name、region、feature_ids; 不再包含嵌套 features |
| features.json | 特征数组, 每项 id、description; 主归属仅由 E.feature_ids 表达 |
| relations.json | 关系数组, 每项 id、participants、description; participants 为 E/F ID 数组 |

Issue 字段与提交中的 refs/description 一致. 数组顺序保留提交顺序, 不以 ID 排序或改号. 回读验证路径范围、实际图像哈希、ID 和类型、每个 F 恰被一个 E 引用、全部 F 均有归属、R 参与项和问题引用合法、status 与 gaps 一致. 纯背景的空 E/F/R 包同样允许, 但 context_notes 须非空. 正文值只有一处主要保存位置, README 为派生视图.

新增并公开 read_observation_package(directory), 返回 tuple[ObservationManifest, SceneObservations], 分别承载上述 manifest 与摊平 E/F/R 集合. 保持 read_report_package / read_sketch 的旧五库返回语义, 不扩为含糊的联合返回类型. 两种正式读取器均拒绝未知或错误协议; 生成入口只预读 schema_version 做分派, 然后交给对应正式读取器校验, 不根据文件是否缺失猜协议.

ResultRecord.analysis_protocol 和 run.json 写真实新协议, 沿用 AnalysisOutcome 的索引与 report_dir. CLI stdout 只承载状态和报告索引 JSON, 诊断写 stderr; completed/partial/failed 退出码沿用 0/1/1, 输入或迁移错误为 2.

生成输入先识别协议: 旧完整包照常读取; 观察包明确说明“尚需 M/S 规划”, 不补空 M/S 或隐式启动规划. 网页按协议展示上下文和 E/F/R, 观察结果不进入草图选择或报告生成提交路径; 旧报告选择继续可用.

网页输入链也必须一起迁移: 去掉“独立探索视角”控件和请求中的 perspectives, _command 不再传 --max-tasks; 分析名称和帮助改为单 Agent 全图观察及有限预算. 服务端对旧 analyze 请求仅接受 perspectives 缺省或旧默认 3 作为兼容占位, 2 明确返回迁移错误, 不能丢弃后假装已执行双视角. 该规则只作用于 analyze 模式, 不改变生成请求的既有行为. Job.snapshot 按 manifest 协议读取, 观察包返回 sketches=[] 及真实报告内容, 不尝试打开不存在的 sketches.json.

## 6. 配置迁移合同

保持 AnalysisOptions 现有 keyword-only 参数名称, 新增正整数 max_model_calls. 默认执行变更为单观察角色, 不增加 mode 开关维护两套路径.

| 字段组 | 单角色行为 |
| --- | --- |
| output_dir、重试/超时、流式配置 | 沿用原含义 |
| max_output_tokens | 唯一角色的输出预算; CLI 显式值 > YAML > 内置默认 |
| max_model_calls | 新正整数预算, 默认 8; CLI 显式值 > YAML > 内置默认 |
| max_tasks、max_parallel、max_measurements | 旧默认 3、3、8 仅为兼容占位; 非默认值启动前报迁移错误 |
| max_worker_calls、max_orchestrator_calls、max_integrator_calls | 旧默认 0 为占位, 新执行仍使用有限 max_model_calls; 非 0 报错并提示新字段 |
| worker_observation_tools | false 为占位; true 报错, 首版未提供取证 |
| outline/worker/integration_max_output_tokens | null 为占位; 非 null 报错, 提示改用 max_output_tokens |

YAML/CLI 可识别显式旧键时, 即使值等于占位值也给迁移诊断. Python 对象无法区分显式默认和省略, 按值判断并在运行配置中标注弃用占位. 诊断写 stderr 和运行记录, 不进入模型输入或 stdout; 旧值不记录成实际生效角色数或预算.

max_tasks=1 不是单 Agent 开关, 须报明确迁移错误. 包内默认 analysis.yaml 只展示有效配置. 保留旧 CLI 参数解析以提供迁移错误或诊断, 帮助标为弃用. 所有迁移错误先于模型构建、网络调用与运行文件创建.

校验分两层: 读取 YAML / 构造 AnalysisOptions 时保留类型、字段名和基本数值范围校验, 移除构造器中的“恰好两个或三个角色”限制; 单观察角色的弃用字段语义校验放在所有显式 CLI 覆盖合并之后、执行入口之前. load_analysis_options 不能因旧字段的合法非默认值提前拒绝, 否则 CLI 无法把它覆盖为兼容占位. Python 直接调用在 run_analysis / run_analysis_task 入口执行同一最终校验. 例如 YAML max_tasks=2 加 CLI --max-tasks 3 可运行并告知弃用, 未覆盖的 2 则启动前报错; 非法类型仍在读取时拒绝.

## 7. 分阶段实施与验收

每阶段完成后记录改动、命令与结果. 模块职责、合同或依赖改变时同步相邻 README. 当前架构文档在代码实际切换后再更新为单角色事实.

| 阶段 | 主要落点 | 完成条件 |
| --- | --- | --- |
| 1 合同与存储 | domain/scene_observations.py、storage/observation_package.py | 多 E/F 归属和混合 R 可读写; 跳号 ID 不变; 非法引用、重复归属、哈希损坏被拒绝; 旧包仍可读 |
| 2 会话与原生角色 | agents/analysis/native.py、workflows/analysis/session.py 和 pipeline.py、角色资源 | 实际工具可提交和修正; 接受后主模型及摘要均无请求; 冲突批次、越权、快照 IO、无提交、预算和中断正确处理 |
| 3 入口与消费者 | entry.py、contracts.py、api.py、配置/YAML/CLI、生成输入、web/ | 协议索引真实; 新读取入口可用; 覆盖合并后校验迁移; 网页输入和展示正常; 发布失败记录可核查 |
| 4 校准与清理 | 五图运行记录、模块文档、失效执行引用 | 五图均合法交付并完成逐图人工语义复查; 清理不用的多角色执行而保留旧包支持 |

阶段 2 捕获实际模型请求, 验证首轮与修正轮原图只有一份且可见、规则样例可读、SDK 外置后历史可回读、工具回执完整配对. 同步和异步终态冲突都覆盖. 测试不以匹配提示词字面量为目标.

结束测试应把历史设置为足以触发摘要, 验证接受后的结束钩子令主模型和摘要模型调用增量均为零; 不仅检查主模型的普通计数. 对快照写入、写包和发布回读分别注入 IO 失败, 确认不会登记成功结果, 且正常可写的诊断位置保留原始错误.

阶段 3 覆盖配置优先级、旧默认占位、旧非默认错误、CLI JSON 与退出码; 网页验证观察展示与草图生成入口的阶段区分. 本次不要求修改生成器或渲染器.

补充 YAML 旧非默认值被 CLI 覆盖为占位的案例, 验证通用传输预算不调用旧阶段解析; 网页测试覆盖默认请求不再传视角、旧双视角请求被拒绝、观察包轮询不读 sketches.json、旧完整包仍可选择草图.

阶段 4 固定模型、原图、调用/输出预算和规则版本. 每图记录运行路径、合法提交/修复次数、耗时、token、遗漏/过拆/重复/关系错误/无依据推断及人工结论. 按粒度含义判断, 不按固定条数或措辞评分. 真实运行按既有模型/追踪配置及已授权范围执行, 不在离线检查中发起.

完成门槛: 五图各有可回读的合法交付, 无运行或交付失败, 且逐图人工复查没有未解决的关键遗漏、重复目标或关系归属错误. 模型自称完成或仅记录问题不算通过. 若仍有关键问题则保留阶段 4 未完成及下一项修正, 不以预算耗尽或五次运行结束代替验收. 五图也作为运行样例可见, 所以此门槛只证明已知案例回归, 不证明未见图片的泛化能力.

在应用目录执行 make repository_check、make test、make lint 和 git diff --check. 仅修改本文时执行文档与 diff 检查. 不改渲染或浏览器链路时无需启动 WebGL2 集成测试. 模拟模型、真实模型稳定提交、视觉粒度验收分开记录.

## 执行清单与当前证据

- [x] 单 Agent、全图多元素 E/F/R 首版范围确认, 规范与五图参考入库.
- [x] 架构 review 完成, 四项风险已落实为本文约定.
- [x] 本次实施方案撰写; 默认值和兼容策略为实施建议, 不代表已有代码行为.
- [x] 实施方案二次 review 与文档修复: 结束钩子顺序、快照/发布失败、包字段、配置合并、网页输入和校准门槛已明确; 对应实现与测试仍未执行.
- [x] 阶段 1: 合同、校验、观察包及旧协议验证.
- [x] 阶段 2: 原生角色、上下文、工具与终态验证.
- [x] 阶段 3: 入口、配置、CLI、网页及生成消费者迁移.
- [ ] 阶段 4: 五图校准、人工审阅与文档清理.

2026-11-10 历史离线探查: 旧单目标探索拒绝 R 引用目录内另一个 E; 纯 F 观察因缺 M 被拒绝. 本轮 review 的模拟模型探查: return_direct=True 在非法提交后结束, False 可继续修正. SDK 并发及历史外置通过本地源码核查. 这些证据不证明新功能已实现或视觉正确.

实施中如需改变公共接口, 记录具体差异和消费者影响, 优先新增入口或兼容包装. 失败尝试与原始错误保留在对应运行记录, 不以删除验证或发布空包推进完成状态.

## 本次方案 Review 与修复记录

本节记录方案缺口的修复, 不将文档修正当成实现已验证:

| 优先级 | 复核发现 | 修正落点 | 后续证据 |
| --- | --- | --- | --- |
| P1 | 内层 wrap_model_call 结束检查可能晚于 SDK 摘要 | 第 3 节使用模型链之前的结束钩子 | 高历史负载下终态后两类模型均不再请求 |
| P1 | 发布回调抛错可能绕过 failed 索引保存 | 第 4 节独立处理快照和发布异常, 明确磁盘全不可写的限制 | 分阶段 IO 故障注入 |
| P2 | 保存字段和读取返回合同尚不够明确 | 第 5 节固定文件字段、返回类型及完整归属校验 | 多元素及纯背景往返、孤立 F 和错误协议拒绝 |
| P2 | 旧配置可能在 CLI 覆盖前被拒绝, 新角色误走旧阶段解析 | 第 2、6 节明确通用配置投影和最终语义校验时机 | YAML/CLI 覆盖和直接 Python 入口测试 |
| P2 | 网页仍有视角控件、参数转发与强制读取草图 | 第 5 节覆盖输入、命令、轮询与兼容行为 | 新观察包与旧完整包网页回归 |
| P2 | 五图阶段原先只要求记录问题, 缺少通过标准 | 第 7 节补合法交付及人工语义门槛, 限定为已知案例回归 | 逐图产物与人工结论 |

方案 review 的依据为 [SDK 中间件装配](../../../../libs/deepagents/deepagents/graph.py)、[SDK 摘要实现](../../../../libs/deepagents/deepagents/middleware/summarization.py)、[配置加载](../../src/shader_deep/workflows/configuration.py)、[发布调用](../../src/shader_deep/workflows/analysis/pipeline.py)、[网页任务](../../src/shader_deep/web/jobs.py). 当时只修正文档; 后续本轮代码实施和实际验证见下节, 两者分别记录.

## 2026-11-10 Subagent 实施与验证记录

三个实现子任务分别负责领域/存储、原生观察链路、配置/CLI/网页; 主任务负责公开入口、生成协议分派、文档与整合. 未改 SDK 或生成器/渲染器, 未增加依赖. 单 Agent 指产品分析运行角色, 不限制本次开发协作使用 subagent.

已实现轻量嵌套提交与摊平 E/F/R 包、单角色原生图、提交修正、受控回读、同批准入、模型链前终态钩子、公开报告索引和消费者协议区分. 运行资源已包含五图样例, wheel package-data 同步纳入普通 Markdown. 旧完整五库读取继续通过测试, 多角色旧执行已退出默认链路.

独立复核另发现并修复: HTTP 校验吞掉旧视角迁移提示; 单个 manifest 诊断写失败阻断其他可写诊断与交付. 对应 HTTP 和 IO 故障测试已加入. 接受快照仍以文件成功写入为前提, 不通过内存状态假装已落盘.

| 检查 | 本轮结果 |
| --- | --- |
| make check | 通过: repository_check 0 findings、260 项单元测试、Ruff/格式/ty |
| tests.integration_tests.test_web | 4 项通过, 真实本机回环 HTTP, 无模型调用 |
| node --check web/assets/app.js | 通过 |
| git diff --check | 通过 |
| WebGL2 集成 | 未运行, 本轮未改渲染/浏览器链路 |

命令在应用目录使用 UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache. 完整整合检查日志位于本机临时文件 single-observer-final-check.log; HTTP 检查由负责子任务执行. 初次整合的旧多角色测试失败已按新行为迁移, 未保留旧方法只为通过旧结构测试.

### 五图真实校准

用户明确授权五图及要求发送至现有 OpenRouter 接口, 模型为 z-ai/glm-5.3-flash; 额外 LangSmith 追踪关闭. 使用固定通用提示, max_model_calls=8、max_request_retries=0、请求超时 45 秒. 输出预算沿用包内 YAML 的 163840, 未暗改用户既有配置. 逻辑轮数不等于所有 HTTP 请求数.

R0 保存点位于仓库 runs/single-observer-calibration-20261110; R1 位于 runs/single-observer-calibration-20261110-r1. 原始图不提交到版本库. 每个 run 保留独立报告和索引; R1/calibration-summary.json 汇总全部尝试、可用指标和对照说明. 下表 run ID 为本机查找入口, 不是可移植的已提交附件.

首轮五图均合法交付, 其中 1 completed / 4 partial. 已发现网格变化归 F、碎片逐色拆分、机制未知误填 gaps、面板柔影遗漏等问题. R0 缺少 token/规则标识指标, 未编造数值. 随后只新增一段提交前归属复查规则, 保留原结果; 同时补充程序指标记录, 不替模型改写报告.

R1 固定 rule_sha256 为 9e9df9f77d51044a2a3a50e67642d14c313729de17e4f65d9b316de15978c699, examples_sha256 为 5a7ad70090b757a97eb6e232649dd998d2bfeb8b0b7cd0b5df3fffe0597aec0f. 指标包括模型名、规则和样例 hash、发布前分析耗时、逻辑轮数、校验/拒绝数和 SDK 实际报告的 token usage; 未报告数据为 null, 不声称完整 HTTP 计数或总成本.

| 图 / run ID | status | E/F/R | 逻辑轮数 | 发布前分析秒数 | SDK 报告 total tokens | 对照参考拆分的审阅 |
| --- | --- | --- | --- | --- | --- | --- |
| 粉色圆 / run-bwqgway4 | completed | 1/5/1 | 2 | 27.15 | 11217 | 外观主要项已覆盖, 随形关系 participants 仍缺轮廓 F1 |
| 网格 / run-lcvtg0ce | failed | 无 | 1 | 15.24 | 未报告 | 连接错误, 未发布空包, 独立保留失败 |
| 网格重试 / run-2a0yv96c | completed | 3/5/2 | 3 | 35.98 | 20895 | 覆盖变化归 R、边缘成组、尺度位置另列 R, 符合参考拆分 |
| Windows / run-4sksun10 | completed | 1/4/2 | 3 | 75.56 | 41343 | 碎片群保持一项 F, 窗格随形与色带延续分别为 R, 符合参考拆分 |
| 磨砂叠圆 / run-u3z1e07p | completed | 2/6/3 | 1 | 21.62 | 10425 | 覆盖变化仍在 F/R 重复记录, 未过粒度门槛 |
| 面板小件 / run-9ktvao_2 | partial | 2/6/1 | 3 | 40.13 | 24871 | 数量/布局误读, 群体与面板布局 R 遗漏, 逐枚遮挡仍误作必要缺口 |

网格只针对连接失败按相同模型、规则和预算重试一次, 没有删除失败尝试. 最终五图均有合法文件包, 4 completed / 1 partial. E/F/R 数量仅用于诊断, 不作为通用评分. 以上为 AI 对照用户已确认参考拆分的审阅, 不等于用户视觉验收, 也不证明未见图片泛化.

阶段 4 的“全部粒度通过”门槛尚未达到. 当前交付证明轻量单角色链路可运行、结构和失败处理可核查; 后续应对剩余三个案例的语义问题继续单假设校准, 保留对照, 不给系统增加自动改写语义或强行清空 gaps 的伪通过逻辑.

### 新五图独立测试: 2026-11-10

用户先要求 Codex 直接分析新五图, 随后要求应用四处小修并用分析 Agent 测试. 修订稿补充 S 图白边旁的暗带, 收紧连接/截断表述, 明确水面近景虚化, 将星形改为静态斜置描述. 修订稿只作为对照, 未加入运行 prompt、examples 或模型输入; 本轮没有修改业务代码和运行规则.

沿用既有 OpenRouter z-ai/glm-5.3-flash、通用分析提示及上一轮测试预算, 关闭额外追踪. 本组两张图曾被自动审批拒绝外发, 收到用户明确的目的地授权后才执行; 拒绝本身不计模型尝试. 五图各启动一次, 额外网络重试配置为 0, 不据此声称已完整计量底层请求. 原图、规则及样例 hash 均与记录一致, 五个报告包均通过 read_observation_package 正式回读.

完整产物保存在仓库 runs/single-observer-new-cases-20261110-uyrhcnj9: experiment.json 固定条件和参考稿 hash, codex-reference.md 保存修订对照, summary.json 汇总逐图产物与审阅. 每个 case 独立保存 CLI 索引、stderr 和原始 run 目录, 未改写 Agent 报告.

| 新图 / run ID | status | E/F/R | 逻辑轮数 | 发布前分析秒数 | SDK total tokens | 对照结论 |
| --- | --- | --- | --- | --- | --- | --- |
| cool-s-distance / run-bb_zhez8 | completed | 2/8/2 | 3 | 228.64 | 63320 | 主体与条纹覆盖, 特征拆分更细; 随形和边线接续关系需复核 |
| flat-water-effects2 / run-y4xzrs0f | partial | 3/10/0 | 1 | 18.58 | 10924 | 反光写成 F, 漏关键 R; 精确直径未知误作必要缺口 |
| dna-helis / run-sxzsa961 | completed | 2/4/2 | 2 | 344.07 | 70929 | 点群/杆群分 E 是替代粒度; 左下和右上节点裁切描述有事实错误 |
| buffer-bloom / run-dpi3qoq6 | partial | 4/7/0 | 1 | 18.11 | 4536 | 漏斜排和光晕交叠 R; 将斜向布局写成垂直, 像素精度误作必要缺口 |
| auroras / run-0ojqu9sv | completed | 4/10/4 | 2 | 147.64 | 27837 | 倒影 R 已捕获; 光帘分区更细、地景单建 E 待确认, 遮挡因果措辞偏强 |

合计 3 completed / 2 partial, 9 次主模型逻辑判断, SDK 已报告合计 177546 tokens. 不将单图耗时相加冒充并行测试总耗时, token 统计也不冒充全部 HTTP 请求或费用. E/F/R 条数只用于定位差异; 替代粒度本身不能据此判错. 本轮由 Codex 对照图像和修订稿审阅, 不等于用户视觉验收.

本轮确认可运行性与文件合同, 未通过全部语义校准: 优先处理交互 R 漏报、无必要精度 gaps、重复/过细拆分和局部事实误读. 测试不自动触发规则调优, 新五图仍未写入已确认运行样例, 阶段 4 保持未完成.

十个 case 的同规则合并统计、逐图问题分类与优化优先级见[十图综合审阅](ten-case-observation-review-20261110.md). 该文仅提出下一轮实验, 不代表复查机制、预算调整或模型切换已经实施.


## Skill 分层重构与后续回归

本次 skill 与按需资源装配的改动、两轮真实回归和未解决项见[分层重构记录](scene-observer-skill-20261110.md). 文件与装配已验证; 语义稳定性仍未通过, 不以 completed 数量升级阶段 4 状态.
