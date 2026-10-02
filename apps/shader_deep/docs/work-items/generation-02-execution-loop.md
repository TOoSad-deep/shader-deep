# 02 生成执行闭环实施方案

日期: 2026-10-03.

## 状态与目标

状态: 待实施. 本文是第二个分支的实施方案, 当前没有据此调用模型或浏览器.

目标: 把 01 固定的五库方案接入现有 DeepAgents 生成、渲染、预览、自检与选择循环, 交付实际 GLSL、预览和可解释的结束原因. 本分支完成后才形成单方案闭环.

共同契约以[生成整体架构](../generation-architecture.md)为准. 现有[生成工作流](../../src/shader_deep/workflows/generation.py)与[真实渲染测试](../../tests/integration_tests/test_generation.py)是复用起点, 不复制一套 Agent 循环.

## 分支和依赖

- 计划分支: `TOoSad-deep/repo/generation-execution-loop`.
- 前置: [01 输入绑定与上下文](generation-01-input-binding.md)合并, 私有准备结果可以提供目录、任务、固定材料和配置.
- 下游: [03 多方案比较](generation-03-plan-comparison.md)逐次复用本分支的报告生成入口.
- 开始时复核 01 的实际接口和旧入口调用方, 保留其他工作的修改; 不改变分析主 Agent 的职责.

## 范围与文件清单

以下路径均相对于 `apps/shader_deep/`, 尚未存在的路径使用代码标记.

| 文件或入口 | 本分支职责 |
| --- | --- |
| `src/shader_deep/workflows/generation_from_report.py` | 调用输入准备和内部执行 helper, 返回现有 outcome |
| [workflows/generation.py](../../src/shader_deep/workflows/generation.py)、[api.py](../../src/shader_deep/api.py) | 复用执行与实际代码读取, 导出报告生成入口, 保留旧接口 |
| [agent.py](../../src/shader_deep/agents/generation/agent.py)、[middleware.py](../../src/shader_deep/agents/generation/middleware.py) | 接入固定材料, 统一模型内外循环的结束判断 |
| [session.py](../../src/shader_deep/agents/generation/tools/session.py)、[finish.py](../../src/shader_deep/agents/generation/tools/finish.py) | 共用准备目录, 实现受限主动停止和会话终态 |
| [contracts.py](../../src/shader_deep/agents/generation/contracts.py)、[prompts.py](../../src/shader_deep/agents/generation/prompts.py) | 补充结束语义与模型可见工具说明 |
| [cli/generation.py](../../src/shader_deep/cli/generation.py)、[README.md](../../README.md)、[AGENTS.md](../../AGENTS.md) | 增加显式报告输入模式, 同步生成工具和使用约定 |
| [test_generation.py](../../tests/unit_tests/agents/test_generation.py)、[集成测试](../../tests/integration_tests/test_generation.py) | 复用模拟模型夹具与真实 WebGL2 案例 |

`render_shader`、WebGL2 渲染协议和单工作线程继续复用. 只有接线确实需要时才调整相邻辅助函数, 不改造整个模型传输层.

## 关键接口与结果

报告生成入口接收 01 的报告选择和执行参数, 内部完成准备后执行, 对外返回现有 `GenerationOutcome`. `PreparedGeneration` 保持私有, 不要求调用方组装新的公开请求对象.

旧 `generate_shader`、`generate_task`、`run_shader`、`run_generation` 保持原调用方式. 旧 PNG 入口保持原尺寸默认值和目录分配路径; 报告入口复用 01 的唯一运行目录. 新增公共参数如有需要, 仅使用带默认值的关键字参数并说明变化.

`stop_generation` 只接收受阻原因、关联候选及下一步建议, 建议限于补充输入或改试方案. 它登记受阻结果并结束本轮, 不切换方案、不派发任务、不伪造已选候选. 候选引用沿用当前任务的正常业务校验.

成功选择、主动受阻、预算耗尽和运行异常使用同一会话结束判断. 详细原因通过既有结果记录及 `run.json` 保存, `GenerationOutcome.stop_reason` 表达结束语义; 异常保留原始诊断和资源清理, 不把异常包装成成功.

`finish_shader` 仍只能选择本轮成功渲染且预览已进入后续模型请求的候选. 结果中的代码读取该候选实际 GLSL 文件, 同时保留自检、方案遵循说明、偏离与未完成项; 程序不判断自然语言机制是否已被代码正确实现.

## 按顺序实施步骤

### 1. 复用准备目录接入会话

从现有工作流提取必要的内部执行 helper, 让报告入口将准备结果交给 RenderSession. 固定材料接入现有 Context Middleware, 旧入口继续走兼容路径.

完成条件: 输入副本、候选和 `run.json` 出现在同一个运行目录; 两种入口均使用现有 DeepAgents 模型与工具循环, 没有复制循环代码.

### 2. 统一终态与受限停止

在会话中明确统一结束判断, 同步修改 Agent 外层循环和 Middleware 的结束分支. 在现有 finish 工具位置增加受限 `stop_generation`, 更新公开工具列表、实际执行白名单与提示词.

完成条件: 主动受阻后不再请求模型或渲染新候选, outcome 没有虚假的选择; 正常完成、预算退出和异常路径保留各自原因. 同一响应中后续工具不能重新打开已结束会话.

### 3. 保留实际预览与有限执行

继续逐轮注入固定材料和最新候选, 保留同批渲染后立即 finish 的拒绝规则. 核对模型计数、SDK 重试与摘要调用的实际范围, 只修复会绕过本轮有限循环的问题.

完成条件: 编译错误可以进入下一轮修复, 成功 PNG 实际进入模型请求后才可选择; 纯文本回复、无效工具调用和渲染失败仍受预算约束. 浏览器在所属线程使用和关闭.

### 4. 接通报告 API 与 CLI

增加明确的报告输入模式, 允许显式选择草图及可选的一项局部备选, 并传入本次要求、背景和渲染条件. 保持原 PNG 加提示词命令可用, 避免输入模式被静默混用.

完成条件: Python 调用者得到完整 outcome; CLI 成功时 stdout 只有选中候选的实际 GLSL, 路径和诊断进入 stderr. 未选候选时不输出伪成品, 保留现有未完成与输入错误退出码语义.

### 5. 验证交付并同步说明

先完成下述自动检查, 再在明确图片、模型使用条件后做一份代表性报告的真实生成验证. 在本记录写明代码、预览、来源、结束原因与未覆盖项, 只把实际落地行为写入当前架构.

完成条件: 有可追溯的一次单方案运行证据; 程序检查、模型自检和用户视觉接受分开报告. 真实服务受阻时可交付代码与已有离线证据, 但将真实样本标为待验收, 不能记为已经通过. 不将本方案撰写视为真实验证已经完成.

## 最小必要验证

新增测试集中在接线和新终态, 优先复用 `GenerationFixture` 及现有错误修复、预算、白名单、同批 finish 和 CLI 回归案例.

| 场景 | 必须观察到的结果 |
| --- | --- |
| 报告方案进入生成, 编译失败后修复并选择 | 固定方案持续可见, PNG 进入实际请求, 最终代码与被渲染文件一致, 运行目录唯一 |
| 模型明确主动受阻 | 原因和建议被保存, 没有 selected_candidate, 内外循环均停止 |
| 预算耗尽或执行异常 | 已有代码与失败信息保留, 结束原因准确, 资源沿原路径释放 |
| 旧 PNG/API 与报告 CLI | 旧调用继续工作, 成功和未完成输出均遵循既有语义 |

保留已有选择资格与白名单测试, 不按每种终态复制全部参数组合. 扩展现有真实 WebGL2 成功案例覆盖报告输入和实际图像反馈, 复用其失败制品保留案例.

分支收口在应用目录执行 `make test`、`make lint`、`make repository_check`、`make integration_test`. 通过后且没有相关修改, 不重复跑同一组检查. 真实浏览器测试使用模拟模型, 不证明外部模型服务或视觉效果达标.

未来人工验收只先选一份代表性报告和一个显式方案, 检查原画布位置、背景要求、方案偏离及预览质量. 实际图片与模型使用条件在执行前明确; 本次文档工作不发起该调用.

## 完成与明确不纳入

完成是报告选择可以经过现有闭环交付实际渲染代码, 或如实交付受阻、预算及异常记录; 同时旧公开入口兼容. 视觉接受由用户另行确认.

不纳入自动换方案、额外视觉评审 Agent、并行生成、恢复引擎、分布式协调、传输层统一重写或多 Pass. 保留正常引用校验、工具白名单、预览回读、失败制品和资源清理.

## 实施记录

待填: 实际代码状态、改动、验证命令与结果、真实样本结果和未覆盖范围.
