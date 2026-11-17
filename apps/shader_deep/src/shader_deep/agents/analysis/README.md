# 分析角色

## 职责与入口

[native.py](native.py) 的 build_analysis_agent 装配一个原生 Deep Agents 全图观察角色. SDK 管理工具循环、消息、摘要与隔离 StateBackend; 业务接受和终态由[观察工作流](../../workflows/analysis/README.md)管理. 角色可分析多个 E, 不再委派探索或整合角色.

[scene-observer/SKILL.md](../../resources/skills/scene-observer/SKILL.md) 是运行规则入口. 这是专用观察角色, 简短的共同流程直接进入 system prompt; 条件式参考文件预置到 StateBackend, 仅在 read_file 返回后进入消息. 已知使用此 skill, 因此不再启用 SDK 的技能发现提示或强制额外读取主文件; 通用 SDK 文件读取与工具循环继续使用.

| 触发条件 | 按需参考 |
| --- | --- |
| 主流程仍无法确定对象/群体边界或 F 拆合 | [grouping.md](../../resources/skills/scene-observer/references/grouping.md) |
| 主流程仍无法确定 F/R 归属、关系拆合或参与范围 | [relations.md](../../resources/skills/scene-observer/references/relations.md) |
| 使用基本分类后仍无法判断歧义的必要性或缺口归属 | [uncertainty.md](../../resources/skills/scene-observer/references/uncertainty.md) |

主文件直接包含背景登记、精简描述、F/R 基本归属及问题/缺口口径, 普通提交无需先读参考才能知道交付要求. 每个参考仅补充复杂判断方法与短边界例子. 未读取参考本身不再等同于违反主流程; 读取记录仍用于诊断, 不代表质量验收. 原[五图完整答案](../../../../docs/analysis-observation-examples.md)仅供离线校准, 不预置到运行时. 文档导航见[观察粒度解释](../../../../docs/analysis-observation-guidelines.md).

## 输入、工具与上下文

| 内容 | 提供方式 |
| --- | --- |
| 用户要求、固定整图、尺寸和剩余预算 | FixedInput 在实际主模型请求中注入一次, 不反复追加持久历史 |
| 主流程和条件式参考 | analysis_input 预置到本图文件状态, 只有 SKILL.md 正文直接进入固定指令 |
| 草稿与产物 | save_observation_draft 首次保存; read_file/edit_file 修订; finish_analysis 冻结接受 |
| 受阻结束 | stop_analysis 记录非空原因, 无伪造成功报告 |
| 材料读取 | SDK read_file 只读本角色资源及本图 conversation_history / large_tool_results |

原始图像实际字节进入模型请求, 路径占位不代表已看图. 仅开放固定草稿文件的局部编辑, 其余资源只读. 原图和规则在当前图中固定; 不把历次报告或模型自由文字自动写为长期记忆.

## 执行边界

- ObservationGuard 的 before_model/abefore_model 先检查终态再计逻辑调用, 因此接受后不会再运行摘要或主模型请求.
- FixedInput 限制可见工具, ObservationGuard 在执行端再次验证白名单, 不依赖 subagents=[] 关闭 SDK 默认角色.
- 同步与异步路径均按 AIMessage.tool_calls 的声明顺序执行. 同批编辑采用前一步提交版本, 读取能看到前一步修改. 完成仍校验最新全文已进入模型请求, 终态后拒绝后续调用.
- 字段结构错误拒绝保存; 领域引用问题允许保留草稿, finish 前完成修复. 最新完整草稿经回读并实际进入后续主请求后才可 finish. IO 与非预期异常进入运行失败, 不伪装成字段错误.
- 原始短 ID 由模型按定位诊断局部修订, 程序不猜测重映射; F 主要归属由嵌套位置派生. 没有 M/S 规划或证据维护负担.
- 主模型调用预算与 SDK 摘要、网络重试不同. 不将逻辑轮数描述为所有 HTTP 请求的总上限.
- 模型只记录安全 model_name. rule_sha256 标识主文件, resource_sha256 标识各资源, skill_bundle_sha256 标识整组资源; 旧 examples_sha256 仅存在于历史运行.
- resource_read_results 只记录受控资源路径及工具返回状态, 不存文件正文. 资源预置、工具成功返回和模型理解是不同事实; 该指标不证明阅读完整或语义遵循.

旧 [observations.py](observations.py) 的证据工具仍供旧证据合同验证使用, 不挂载到新观察角色.

## 验证

[原生图行为](../../../../tests/unit_tests/agents/test_native_analysis.py)覆盖实际图像、修正、权限、资源回读、批次冲突、预算、终态早于摘要及异步执行. [旧证据验证](../../../../tests/unit_tests/agents/test_reference_observations.py)继续保护已保存完整包的证据取材, 不恢复已删除的多角色执行.


## 草稿后端

[draft_backend.py](draft_backend.py) 将共用 [DraftBackend](../draft_runtime.py) 固定到 observations.json, 作为 CompositeBackend 的 /draft/ 路由. 默认 StateBackend 继续管理规则与 SDK 外置材料. 草稿只存磁盘, 读取与编辑调用同一 [DraftStore](../../infrastructure/storage/observation_draft.py). 编辑结果反馈包含版本、差异和诊断.

ObservationGuard 为共用 [DraftToolGuard](../draft_runtime.py) 配置观察工具与文件范围. 共用辅助负责同批顺序执行、执行端权限和终态检查. 观察角色继续管理自己的输入、领域校验、接受快照与交付.

FixedInput 绑定请求草稿版本, AnalysisSession 通过共用 [DraftReadTracker](../draft_runtime.py) 累计实际进入主模型请求的同版本行范围. 回执保存正文哈希与行范围. 编辑后完整回读最新版本, 正文实际进入后续请求时才更新已见版本.

观察与规划使用同一套草稿运行辅助. 规划工具、规则及 M/S 接受流程见[规划角色](../planning/README.md).
