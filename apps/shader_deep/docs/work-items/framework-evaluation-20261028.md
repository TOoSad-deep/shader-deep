# 分析框架替换首轮评估

> 历史记录: 下文保留当时的实现、命令与验证结果. 旧分析执行和回放已退出当前包, 最新范围见[清理记录](analysis-legacy-cleanup-20261028.md).

用户随后明确以代码简洁和开发便利为主要目标, 并选择 Deep Agents 原生能力优先. 当前实施方向见[原生优先方案](deep-agents-native-plan-20261028.md), 中间分析见[开发简洁性复评](framework-simplicity-review-20261028.md); 本文保留首轮实验事实, 不再以性能或真实模型配对作为后续简化的主要决策门槛.

- 日期: 2026-10-28, Asia/Shanghai.
- 状态: 完成源码核查、离线行为对照和有上限的真实模型试验; 不切换默认实现.
- 范围: 受限探索 worker 的装配工厂, 以及 SDK 子 agent 派发的适配成本. 主角色、TaskStore、报告契约和生产代码保持原样.
- 前置工作: [分析模块架构整理](analysis-architecture-refactor-plan-20261028.md).
- 可复核数据: [指标与失败摘要](evidence/framework-evaluation-20261028.json).

## 1. 结论

**本轮不支持整体替换框架或直接切换默认 worker. 局部用 `create_agent` 装配受限 worker 值得继续验证, 主要收益是减少不需要的默认能力, 尚无真实模型性能或质量优势证据.**

Deep Agents 本身也调用 LangChain `create_agent`, 两者仍运行在 LangGraph 上. 当前候选是在同一执行基础上减少 harness 默认行为, 不是换一套执行引擎.

| 候选 | 首轮判断 | 原因 |
| --- | --- | --- |
| 保留当前 Deep Agents 装配 | 保持默认 | 已有业务控制与完整工程回归; 本轮没有证据要求立刻更换 |
| 受限 worker 使用 `create_agent` | 有条件继续 | 51 项复用行为测试通过, 首轮请求与现有实现相同; 装配更轻, 但长回执处理不同, 真实完成等价性未验证 |
| 主角色也改用 `create_agent` | 暂缓 | 主角色实际使用 SDK SkillsMiddleware 和读取门禁, 本轮没有验证其替代装配 |
| 用 SDK `task` 替换宿主派发 | 当前不建议 | 需重新适配固定输入、单批限制、TaskStore 回执、预算、恢复和状态过滤, 不能仅靠启用 SubAgentMiddleware 获得等价行为 |

不应把真实试验中没有通过的提交归因于工厂: 输入请求相同, 试验还受到记录器错误和上游容量故障影响, 且没有有效的完整对照配对.

## 2. 依赖与源码事实

实际安装版本: Deep Agents 0.7.13（本仓库可编辑源码）、LangChain 1.4.0、langchain-core 1.6.2、LangGraph 1.2.11、langchain-openai 1.6.0. 摘要同时保存本地 SDK 相关源码 SHA-256, 不仅依赖版本号.

1. [SDK 装配](../../../../libs/deepagents/deepagents/graph.py)最终调用 `create_agent`, 默认装配文件处理、子 agent、摘要和工具历史修补等能力.
2. 当前分析装配（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/execution.py`, 已删除）使用 `create_deep_agent`, 关闭自动摘要; 应用中间件（历史路径 `src/shader_deep/runtime/middleware.py`, 已删除）覆盖实际角色提示与工具列表, 并检查执行白名单.
3. 主角色请求策略（历史路径 `src/shader_deep/compatibility/fixed_five_analysis/roles/orchestrator_loop.py`, 已删除）读取 `skills_metadata`, 核对 skill 正文呈现后才开放业务动作; 本轮候选不替换这条链.
4. [SDK 子 agent](../../../../libs/deepagents/deepagents/middleware/subagents.py)默认 isolated 模式用任务描述建立消息, 同时按排除规则传递其他状态; fork 模式继承父会话. isolated 不等于自动满足应用的固定原图与状态隔离合同.
5. SDK 的 `CompiledSubAgent` 要求返回含 messages 的状态, 再将结构化结果或最后一条非空 AI 正文包装为工具回执, 并可合并其他未排除状态. 它不会自动以本应用的持久提交回执判断任务成功.

因此, 即使后续采用 CompiledSubAgent, 仍需保留业务适配器. 让父模型自由调用通用 task 不能替代当前一次冻结、整批验证和受控恢复.

## 3. 候选与试验协议

试验脚本位于 [evaluate.py](../../../../runs/framework-evaluation-20261028/evaluate.py), 四项调度位于 [run_trials.py](../../../../runs/framework-evaluation-20261028/run_trials.py). 它们是隔离的评估制品, 没有导入生产默认链; 不在同一生产进程中使用其 monkeypatch.

候选仅拦截包含 `submit_exploration` 的 worker 装配, 改用:

```python
create_agent(
    model=model,
    tools=loop.tools,
    system_prompt=prompt,
    middleware=[SummarizationMiddleware(model=model, trigger=None), loop],
)
```

应用运行控制、提交工具、提示词、图像输入、结构校验、公共消息驱动、调用限制和网络恢复适配器都保持相同. 主角色、恢复与整合继续走现有工厂. 本轮没有验证分步探索 `finish_exploration` 的工厂替换.

真实模型沿用已有 OpenRouter 配置 `z-ai/glm-5.3-flash`, 未改用户配置或显式设置新的 reasoning 参数. 未固定到某个上游 provider, 所以不是严格的后端性能基准.

| 条件 | 固定值 |
| --- | --- |
| 样本 | 128×128 青色圆角发光边框工程合成图, 单一样本 |
| 顺序 | A: Deep Agents → B: create_agent → B → A |
| 重复 | 计划每条件两次; 失败保留, 不重跑 |
| 输入 | 相同原图 SHA-256、要求、目标、范围和方向 |
| 工具 | 仅默认 `submit_exploration`; 无额外裁剪/测量 |
| 单项额度 | 最多两次逻辑模型调用, 每次最多 8192 输出 tokens |
| 网络 | 零额外重试, 请求超时 90 秒, 每项墙钟上限 240 秒 |
| 总额度 | 最多八次逻辑调用; 实际累计计数七次 |
| 判定 | 先看请求和业务约束, 再看合法提交; 不以程序成功代替视觉验收 |

[冻结协议](../../../../runs/framework-evaluation-20261028/protocol.json)记录版本、输入、预算及生产源码指纹. 运行后核对这些生产文件均未改变.

## 4. 离线结果

### 4.1 复用的行为测试

候选仅替换探索 worker, 复用了以下模块, 共 **51 项通过**:

- `tests.unit_tests.workflows.test_five_analysis`
- `tests.unit_tests.agents.test_main_agent`
- `tests.unit_tests.agents.test_analysis_evidence_review`
- `tests.unit_tests.workflows.test_analysis_focus_recovery`

覆盖包含默认业务链、独立输入、有限恢复、部分交付、主请求权限和既有证据规则. 主角色本身仍使用原工厂, 这些结果不证明主角色也能迁移, 不等于所有 600 项都在候选工厂下验证.

### 4.2 装配成本

在同一进程交错构造四组, 每组五次, 取组内中位数:

| 工厂 | 两组装配中位数 |
| --- | --- |
| `create_deep_agent` | 6.67 ms、6.35 ms |
| `create_agent` | 1.24 ms、1.20 ms |

这只测本地装配, 使用假模型, 不含网络、模型推理和报告生成. 每个 worker 约省五毫秒不能解释几十秒的模型运行耗时, 也不能声称用户可感知提速.

### 4.3 大工具回执不是等价行为

[离线探针](../../../../runs/framework-evaluation-20261028/edge_probe.py)提供一个 90024 字符、末尾带验证标记的工具回执, 再检查第二次假模型请求中实际收到的正文:

| 工厂 | 模型收到的回执 | 末尾标记 |
| --- | --- | --- |
| 当前 Deep Agents | 1607 字符的外置预览 | 缺失 |
| 候选 create_agent | 90024 字符的完整正文 | 保留 |

原因是 SDK [FilesystemMiddleware](../../../../libs/deepagents/deepagents/middleware/filesystem.py)默认处理超大工具输出, 而候选没有安装它. 当前主角色已显式关闭外置处理, worker 装配没有做同样覆盖.

该探针证明行为不同, 不证明本轮真实默认 worker 已发生正文丢失: 它当前只开放提交工具, 不是探针里的大正文读取工具. 两种探针都允许假模型结束, 也不代表它们都有完整证据可用.

替换前须对正文按 ID 读取、证据图像和分步实验明确策略: 全文应保留、受控分页, 还是可外置回读. 若保留 Deep Agents, 也需要保证外置正文可通过当前允许的能力回读; 不能把不可读路径当成已呈现正文.

此外, 候选不含 SDK 的 [PatchToolCallsMiddleware](../../../../libs/deepagents/deepagents/middleware/patch_tool_calls.py). 应用已有非法参数清理和消息配对处理, 但本轮没有覆盖所有中断历史与长回执组合, 不据此声称完全可移除默认能力.

## 5. 真实模型结果与证据边界

| 项目 | 工厂 | 逻辑计数 | 耗时 | 结果及解释 |
| --- | --- | --- | --- | --- |
| 1 | Deep Agents | 2 | 67.97 s | 首次提交因未标记机制引用被拒; 准备第二轮时记录器处理 null 内容出错. 不参与端到端比较 |
| 2 | create_agent | 2 | 93.41 s | 两次提交的 evidence_links 均引用无效对象, 两次调用预算耗尽; 没有合法结果 |
| 3 | create_agent | 2 | 87.90 s | 首次提交因 evidence_links 被拒; 同样受旧记录器的 null 内容错误影响. 不参与端到端比较 |
| 4 | Deep Agents | 1 | 114.73 s | 上游返回容量不足 APIError, 没有可用报告或 usage; 不作质量或速度比较 |

四项均未形成被接受的报告, **没有有效的完整基线/候选配对**, 因此不计算“替换后的成功率提升”“平均速度提升”或“质量优劣”. 两次调用上限是本轮试验约束, 不代表默认流程的最终完成能力.

### 5.1 能确认的请求事实

- 四项首轮完整请求的规范化 SHA-256 完全相同: `d55cfd5ac8960d29237086d447999eb281ba90a4671c87bde363ccd400671f61`.
- 四项首轮序列化请求均为 13382 bytes, 实际可见工具均为 `submit_exploration`, 含一张图像.
- 有 usage 的三项首轮输入 token 为 2123、2123、2121. 相同请求的提供方用量有小幅差异, 不将其解释为框架收益.
- 后续历史取决于模型输出和拒绝反馈, 不能把不同的第二轮体积归因于装配工厂.

这支持一个有限结论: **当前受限 worker 的首轮请求不会因直接使用 create_agent 自动变小.**

### 5.2 记录器错误与用量

初版记录器假设每条消息的 content 可迭代, 没有处理 assistant 工具调用消息中的 null. 项目 1、3 在准备第二轮摘要时因此失败; 该错误不属于 Deep Agents、LangChain 或业务提交校验.

错误已修正, 并用 null assistant 消息和图像块做无网络验证. 保留初版脚本、修订指纹与[修正说明](../../../../runs/framework-evaluation-20261028/measurement-correction.json). 不追补已失败项目, 不把修正后的结果混入原始配对. 项目 2 完成了两轮请求记录; 项目 4 使用修正后的记录器但遇到上游故障.

本轮累计七次逻辑调用计数, 完成构造并记录五个请求 payload, 收到四条有效 usage. 已报告输入 11049 tokens、输出 21295 tokens（包含提供方报告的 reasoning）. 逻辑计数包括两次记录器失败, 不能当成七次服务端完成; 没有 usage 的故障不能按零成本处理. 未取得完整计费金额, 不估算费用或节省比例.

## 6. 下一步建议与切换门槛

1. **保持主角色和宿主调度.** 当前实际依赖 skill 发现、权限门禁与 TaskStore, 优先保留已验证合同. 不将 SDK task 迁移和 worker 工厂精简混为一次改动.
2. **保留 worker 候选作为显式试验.** 若继续, 将一次性装配替换转成范围清楚的实验选项; 默认仍为现有实现, 任务、协议和数据格式不动.
3. **先处理可比性.** 使用修正后的记录器, 等服务可用时获得合法报告的配对样本. 不为“让某条件通过”单独放宽预算或修改提示词. 引用/evidence_links 可靠性属于两种工厂共用的业务契约问题, 需要单独诊断.
4. **补关键行为而非大量测试.** 针对大回执、消息配对、中断/修复、分步读取及证据呈现做少量定向验证, 然后再考虑在候选配置下运行现有完整回归. 无需为每个默认中间件写一套结构断言.
5. **明确采用理由.** 若后续行为等价且维护复杂度降低, 可以以“减少不需要的默认能力”为理由迁移受限 worker. 当前数据不支持以省 token、提速或增强视觉推理为理由切换.

切换默认前至少满足: 固定输入与合法输出配对成立; 权限、预算、恢复和已呈现材料约束无回归; 大回执策略可解释; 安装包与历史入口通过; 失败与成本没有被试验早退掩盖. 若这些条件未满足, 保留当前实现.

## 7. 复核与重现

本轮原始制品在仓库 `runs/framework-evaluation-20261028/`: 冻结样本、脚本、日志、四项 TaskStore 记录、工具拒绝反馈与 summary. `runs/` 不代表已进入 Git; 可持久审阅的摘要位于本文件及 evidence JSON.

离线前检的实际命令（在应用目录执行）:

```sh
PYTHONPATH=. uv run --no-sync python ../../runs/framework-evaluation-20261028/evaluate.py preflight
```

真实运行使用既有 `.env` 注入配置, 其值不写入报告. 已有 trial 目录拒绝覆盖; 如进行下一轮, 使用新目录、记录新协议及预算, 不能直接对已完成清单反复执行. 默认生产源码及应用锁文件指纹在试验后核对未改变.
