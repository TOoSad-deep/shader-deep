# 独立探索与可能性库

当前公开入口 `run_analysis` / `run_analysis_task` 和 `shader-deep-analyze` 使用 `possibility_library_v1`. 公开 Python 参数和 CLI 用法保持不变. 旧报告类型仍可识别与校验, 但公开入口不运行旧的观察/解释综合流程, 也不自动转换旧报告.

## 角色与阶段

角色分为主 Agent、探索子 Agent 和整合子 Agent. 主 Agent 负责初稿规划; 后端等待独立探索全部终止后, 同步委派一次整合子 Agent, 最后由协调器检查整轮交付状态. 各角色独立维护模型历史与执行记录, 初稿与整合仍共用 `max_main_calls` 总额度.

| 阶段 | 业务输入 | 业务输出与推进方式 |
| --- | --- | --- |
| 主 Agent 初稿规划 | `user_request` + 完整原图 | 一次 `submit_visual_outline(outline, directions)`, 提交统一初稿和至少两个中性探索问题 |
| 探索子 Agent | `user_request`、`visual_outline`、`exploration_direction` + 完整原图 | 一次提交草图、特征、关系三个库; 后端直接校验入库 |
| 整合子 Agent | 问题复核需要原图及完整材料, 发现和比较使用当前文本 | `review_outline` / `verify_outline`, `plan_comparisons`, 各小组 `submit_integration`; 后端顺序推进并检查终态 |

初稿通过后冻结本轮元素和实例组身份. 后端按 `directions` 注册任务, 在并发额度内运行, 全部任务终止后进入整合; 整合阶段不再自由派发子任务. 探索子任务各自保留独立历史, 不接收兄弟报告、主任务聊天或测量材料. 无效草稿可局部修复, 初稿问题可单独反馈, 不依赖成功报告承载.

## 独立整合子 Agent

完整图片入口和标准冻结回放共用 `ManagedIntegrationSession` 协调器, 通过 `IntegrationInput` 委派 `IntegrationSubagent`, 不依赖固定样本 ID. 子角色接收固定业务快照、初稿版本、反馈、原图和受控库服务, 不接收主 Agent 的聊天历史、工具或修复会话. 整合结果通过 `IntegrationOutcome` 返回, 包含修订、比较工作、库版本、缺口和独立执行记录. 库更新仍由原有受控工具发布, 整轮 `ResultRecord` 仅由协调器生成.

整合首先集中处理所有初稿问题, 再从本轮拥有者目录发现有依据的小组, 按特征、关系、草图顺序比较. 候选发现随后读取实际规范拥有者及全部候选, 也包括未参与拥有者合并的条目, 再安排必要比较组. 每组 2 至 6 个同类型目标, 同一发现阶段每个目标只安排一次. 工作组数量由现有总阶段/总调用额度控制, 不另设 6 组上限. 候选发现会接收已有拥有者比较结论, 不把已明确保留的不同拥有者强行合并作为前置义务. 发现不等于已经比较; 未选对象原样保留, 不声称覆盖全部组合. 必要发现未完成时必须记录 `deferred_work`.

普通比较共用 `ControlledSession` 的完整正文、实际呈现与版本检查. 工具只允许当前类型的合并、保留或整组暂缓, 不接收任意新增、引用修复、范围扩展或元素修改. 因引用冲突无法发布时保留完整草稿并暂缓, 不强行扩大事务. 特征比较范围和外观, 合并保留候选并集; 候选另行比较同一规范拥有者下的机制和必要前提. 材料不递归展开整库, 未呈现的正文不能从 ID 名称猜测.

`review_outline` 必须逐项处置已呈现问题. `element_features` 只替换问题所涉及已有元素的显著外观文字, `relation_descriptions` 按零基索引修改相关已有关系描述. 身份、对象范围、实例和关系端点保持不变; 新增/拆分对象需求仍暂缓. 接受修订需要完整初稿、原图和库材料已进入请求, 复用库校验和版本提交, 保留原始探索输入与报告. 修订或驳回提案先进入 `verification_pending`, 下一请求由 `verify_outline` 核对原文、修订、反馈与原图; 未确认不能关闭问题. 自动确认及同轮修订加确认不被允许.

每个发现、问题处理、复核和比较步骤都有有限调用预算, 请求中明确显示剩余调用数. 每项只执行一次; 局部超限或无进展后继续独立项, 全局调用/阶段数不足时记录未执行项. 不重置修复计数或自动重入暂缓项. 完整发现目录或问题材料超预算时整阶段暂缓, 当前没有跨页覆盖算法. 后端最终仍检查失败任务、未解决问题、必要工作与覆盖缺口, 不用删除义务换取 `completed`.

子角色每次检查点保存后向协调器发送复制后的进度, 同步刷新主 `run.json` 的计数、比较、初稿版本、问题处置与缺口, 不等待整个整合结束; 完整子角色缺口按快照替换, 避免重复追加.

`integration-subagent.json` 保存独立整合身份、父任务、执行状态及业务进度, `integration-work.json` 保存各步骤结果; `run.json` 和库快照保留原始报告、初稿版本及复核记录. 默认发现和普通比较不发送整图、不开放测量, 只有初稿及问题核对使用原图. 下面的按需目录、测量和宽提交接口属于保留的底层兼容会话, 不再是默认模型可调用工具.

## 兼容按需会话的材料与版本

`integration_materials` 默认提供四库轻量 `catalog`、当前 `comparison`、必要工作 `progress` 与问题 `context`. 初始没有比较正文; 活动比较期间不重复注入无关目录首页. 初稿元素只出现一份, 显式选入完整元素时不再重复初稿中的同身份元素. 问题上下文只展开初稿问题、失败任务及未覆盖元素的有界首页, 完整记录保存在后端.

`list_library` 支持按库、元素、直接端点 ID、处理状态筛选及游标分页. 默认页最多 30 个拥有者或元素, 返回总数、剩余数和 `next_cursor`; 指定 `owner_ids` 可发现所属候选身份. 目录省略机制和理由正文, 不由模型生成摘要. 游标绑定库版本、查询范围和当前状态; 发生变化时拒绝旧游标并要求重新查询, 不静默跳过条目.

所有目录同时受 `library_page_chars` 的完整 JSON 字符预算限制. 单个库索引过大时保留身份、类型、计数和 `details_omitted`, 完整正文仍通过 `read_library` 查询; 不返回循环空页游标. `list_comparison_work(statuses, work_id, cursor, limit, target_cursor)` 提供必要工作分页, 指定 `work_id` 后可用 `target_next_cursor` 接续目标索引. 超长问题或原因明确标记 `details_unavailable_due_to_budget`; 接续工作时完整理由和原缺口进入当前比较的 `work_context`, 不凭省略后的索引关闭工作. 工作游标绑定查询和完整台账状态.

`list_analysis_context(section, cursor, limit)` 的 `section` 为 `outline_issues`、`failed_tasks` 或 `uncovered_elements`. 初稿问题只有完整内容实际呈现后才能提交处置, 超限身份占位不算已读. 默认页或查询页未显示的必要工作、问题和失败记录仍参与最终完成检查.

`read_library` 用 `requests` 一次选择一个或多个库的多个 ID. 草图只返回自身组成与引用; 特征和关系默认只含自身字段及候选 ID. `include_candidates=true` 显式读取候选, 可用 `candidate_ids` 限定且校验归属. 引用、关系端点和 `requires` 不递归展开. 同组读取按 ID 和版本去重, 后续读取追加材料, 不覆盖此前选材.

首次读取同时提供 `comparison: {question, target_ids}` 建组; 后续补读省略 `comparison`, 已有必要工作用 `work_id` 接续. 比较目标与辅助材料分开记录; `extend_target_ids` 显式扩大目标, 辅助读取不自动获得语义修改权限或完成状态. 拥有者比较不代表所有候选或所有跨条目比较已完成. 只有正文实际进入准备发送的请求才登记呈现; 回执和选材缓存均不证明已读.

新建、接续和扩展范围在工作副本预演: 目标数、必要正文、接续理由与请求预算全部满足后才接受, 否则回滚身份、目标、版本及选材. 候选目标还要求显式请求其拥有者与直接判断前提; 不自动递归加载. 同组辅助正文可按完整条目部分接受, 回执区分已选、重复和未提供项. 超过目标上限的既有必要工作保持待处理或暂缓, 当前没有自动拆分工作工具.

同组保留正文、测量和局部修复历史. 成功提交或明确暂缓后释放正文与修复历史, 仅保留轻量目录、决定和未完成范围. 实质改变此前判断依据时创建必要复核; 纯别名和机械引用重写不要求全库重读. 新增条目默认在本批完成, 需要后续判断的内容通过 `review_ids` 登记.

## 兼容按需会话的提交工具与修复

首次观察开放 `submit_visual_outline`、`repair_analysis_submission`、`stop_analysis`. 子任务开放 `submit_exploration`、`repair_analysis_submission`、`report_outline_issue`、`stop_exploration`.

`submit_exploration` 的三个顶层参数是 `sketch_library`、`feature_library`、`relation_library`, 不再套一层 `report`. 后端将局部 ID 隔离为任务命名空间, 保留元素 ID 和来源. 成功提交后结束子任务; 同内容重放返回原回执, 不同内容不能覆盖已接受报告. 并行探索的写锁只覆盖提交临界区.

整合开放 `list_library`、`list_comparison_work`、`list_analysis_context`、`read_library`、`submit_integration`、局部修复、`measure_reference`、`read_measurements`、`finish_analysis` 和 `stop_analysis`. `submit_integration` 使用以下顶层字段, 不套一层 `decision`:

| 字段 | 用途 |
| --- | --- |
| `merges` | `{kind, members}` 指明明确重复项, 后端稳定选择保留 ID |
| `reference_updates` | 调整指定完整草图的引用范围或局部优先级, 保留组成与元素范围 |
| `additions` | 三库形状的新候选或跨报告组成, 不覆盖原组成 |
| `unresolved` | 按元素 ID 保存具体未解问题 |
| `outline_issue_decisions` | `{issue_id, disposition, reason}`, 选择 `dismissed` 或 `deferred` |
| `review_ids` | 明确需要后续比较的既有条目或本批新增局部 ID |
| `deferred_work` | 本轮未能完成的整合工作说明, 使交付为 `partial` |
| `preserve` | `true` 表示保留本组未修改目标; 可与修改同批提交, 与 `deferred_work` 互斥 |

没变化的字段省略. 先校验目标权限、呈现字段与版本, 再在私有副本应用合并、引用修复和新增. 操作自身的归属与适用范围仍即时检查, 跨对象约束在整批最终状态统一检查. 因此合并产生的临时引用冲突可由同批修复解决, 不在修复前提前失败. 成功后一次发布库版本、来源、回执和本组接受进度. 同一提交身份携带相同内容返回原回执, 携带不同内容拒绝覆盖. 失败不发布部分修改, 不推进比较进度.

同组内保留模型历史与决定草稿. `repair_analysis_submission` 使用与提交参数一致的 JSON Pointer, 例如 `/merges/0/members/1` 或子报告的 `/feature_library/0/candidates/0/reasoning`. 整批成功或明确暂缓后才允许建立下一组. 不提供删候选或中途修改初稿工具.

主任务格式修复按 `outline`、`discovery` 或稳定 `work_id` 分别限制为两次调用, `format_repair_calls` 继续记录全局累计值, `format_repair_scopes` 保存各作用域计数. 同一工作刷新材料、重建循环或暂缓后接续不会获得新额度. 业务拒绝与预算拒绝不冒充格式修复; 主任务累计调用限制仍跨阶段生效.

请求副本仅整理已经实际呈现的成功目录/读取回执, 保留调用与响应配对及未消费结果. 当前完整草稿与错误在控制上下文中提供, 被它替代的历史提交参数和已消费修复副本可简化; 完整交互保存在制品中, 不增加模型摘要调用. 预检发现完整草稿和必要判断材料仍无法容纳时, 保存 `repair_context_exceeded` 并暂缓当前组, 不截断草稿、不发布部分修改, 可继续其他工作.

初稿问题的 `dismissed` 必须有具体理由; `deferred` 保留修订需求并使本轮 `partial`. 两者均不修改冻结的元素身份, 不以文字回执声称完成修订. 需要新初稿的工作留待另轮执行.

## 测量与公共预算

保留的按需会话可批量调用 `measure_reference`; 默认受限比较不开放此工具. 本组请求的全部测量结果及裁剪图自动共同进入后续请求, 不需逐项 `read_measurement`, 后续选择不会覆盖前面的结果. 新测量必须实际呈现后才能接受本组决定. 缓存规格复用与独立测量额度保留; 额度耗尽后隐藏测量工具. 额度耗尽后仍可用 `read_measurements(evidence_ids)` 批量复用此前组证据, 选中结果会共同呈现且不消耗新额度. 子任务输入不包含测量.

当前配置在 [resources/analysis.yaml](../src/shader_deep/resources/analysis.yaml), CLI 显式参数优先于 YAML, 再回落到 `AnalysisOptions` 默认值. 主任务另受以下有限边界控制, 即使 `max_main_calls=0` 也不会取消这些限制:

| 参数 | 当前默认值 | 含义 |
| --- | --- | --- |
| `max_context_tokens` | 262144 | 主任务完整请求的应用估算预算, 包含输出预留 |
| `request_image_tokens` | 4096 | 每张图像的估算预留 |
| `request_token_margin` | 2048 | 请求编码和估算余量 |
| `max_comparison_targets` | 12 | 单组显式目标数上限, 不静默丢弃超限必要工作 |
| `library_page_chars` | 8000 | 库目录、工作和问题索引单页 JSON 字符上限 |
| `integration_history_tokens` | 16384 | 补读时为本组决定与局部修复历史保留的估算空间 |
| `max_integration_calls` | 12 | 每个比较组及初稿阶段的调用上限, 包含修复 |
| `max_integration_packages` | 24 | 比较组处理次数上限, 包括复核 |
| `integration_no_progress` | 3 | 未显式配置正数全局无进展阈值时采用的阶段阈值 |

输出额度由同一解析器同时供模型参数和预算检查使用. `outline_max_output_tokens`、`integration_max_output_tokens`、`worker_max_output_tokens` 分别作用于初稿、整合及探索子任务; 对应 CLI 采用连字符名称. 优先级为阶段 CLI > 通用 `--max-output-tokens` CLI > 阶段 YAML > 通用 YAML > 内置默认值. 当前 YAML 的三个阶段值均为 `null`, 回退 `max_output_tokens: 163840`; 没有自动启用实验额度. `phase_output_budget` 事件记录实际额度与来源.

发送主请求前检查系统提示、工具 Schema、当前业务材料、测量图像、修复历史和输出预留. 文本按每字符一个估算 token 加图像与额外余量计算, 这是应用采用的保守估算策略, 不是提供方 tokenizer 的精确计数或真实上下文窗口声明. 真实用量只读取提供方返回值. 补读先估算追加后的完整请求, 累计扣除固定输入、已有正文与历史、测量图像、工具 Schema 和输出及修复预留. 只追加预算内完整条目, 回执分别列出已选取、重复和未提供 ID 与原因; 同轮多次读取共享累计额度. 选材前同时预留本次回执在工具历史和控制上下文中的开销; 若完整 ID 回执本身已超出剩余额度, 返回简短错误且不追加正文, 可在当前组缩小 `ids` 或 `candidate_ids` 后重试. 测量及裁剪采用相同准入边界. 拒绝过量补读时保留原组选材, 不将未选入正文登记呈现. 发送前仍检查完整请求; 必要最小范围无法容纳或修复历史耗尽空间时保存该范围并交付部分结果.

`max_main_calls` 和 `max_worker_calls` 为 0 表示没有额外的对应累计调用上限; 正整数仍可设置. 探索子任务保留自己的调用预算, 不套用整合比较组预算. 整合派发扣除初稿已用调用, 即使剩余额度为零也不重置为无限制. `run.json` 中 `coordinator_execution` 和 `integration_execution` 各自计数; `main_execution` 为兼容统计保留两者之和, 请求和反馈索引相应转换为累计口径. 网络重试只包围模型响应获取, 不重放已经执行的业务工具. 探索中断通过取消标记阻止新的请求、重试及提交, 已在途的同步请求仍需返回或超时后释放线程; 已入库报告与问题会保留.

## 数据与完成状态

最终 `ResultRecord.analysis_detail` 是 `PossibilityLibrary`, 恰好含四个顶层数组:

```json
{
  "elements": [],
  "sketch_library": [],
  "feature_library": [],
  "relation_library": []
}
```

空数组仅展示形状. 版本由外层 `analysis_protocol` 指定, 模型不重写完整最终库. 最终 CLI 业务正文省略可选空值, 必需数组保留.

- 特征的 `appearance` 表达外观, 候选的 `mechanism` 表达形成方式.
- `organization` 是组织关系; `dependency` 指定影响源与目标.
- `requires` 是选择前提, 单项内为或、多项之间为且; 直接校验不求解全部组合.
- 子草图使用 `candidate_ids`; 后端转换为最终 `candidate_refs`, 优先级仅表示尝试建议.
- 未引用、未排序或低优先级候选仍保留.

默认路径在整合子 Agent 返回后由协调器检查当前比较、必要工作、问题和任务状态; 下述兼容按需会话仍通过 `finish_analysis` 请求交付. 活跃比较或未处置的必要复核不能跳过; 已明确暂缓的必要工作使交付为 `partial`. 不要求模型逐条回读整库或另写交付正文, 未展开内容原样继承, 不宣称已比较. 失败子任务、未处置或暂缓的初稿问题、未覆盖元素、预算出口和显式未完成工作都会保留, 并使交付为 `partial`. 全部子任务均没有合法报告时保留失败状态与制品, 不伪造成功库. 已合法提交的部分内容可以保留交付, 不通过删除失败记录获得全部成功.

`completed` 只说明当前协议流程完成, 不证明机制真实、全部比较完成、所有候选组合可实现或视觉效果已被用户接受. 当前没有分析到生成的自动交接, 候选仍需编码、真实渲染和视觉验收. 此文档不代表两张真实图片已经通过新版运行验证, 也不声称 token 收益已经测得.

## 运行制品与示例

运行目录保留原图、`run.json`、`events.jsonl`、`tools.jsonl`、提交草稿、测量和 `possibility_library/` 中的原报告与版本快照. `tools.jsonl` 保存初稿与整合工具调用的完整参数、结果和调用归属; 整合记录带子角色身份、父任务与累计调用序号, 成功及拒绝均记录; 历史整理不会改写这些原始交互. 库内容、批量提交回执和接受进度以库快照为权威; `run.json` 保存运行状态、缺口、比较工作及问题处置. 这些制品不等于可恢复完整模型历史的会话检查点.

`request_sizes` 记录系统提示、工具结构、消息正文字符数、图片数及工具历史大小. `model_usage` 记录提供方实际返回的用量; 未返回时保持不可用, 不把图像 data URL 长度计为文本 token.

在 `apps/shader_deep/` 可从已保存的冻结输入单独回放整合子 Agent:

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.replay /absolute/path/source-run \
  --output-dir runs/replay --max-main-calls 8 --integration-max-output-tokens 32768
```

源目录需包含受支持的 `run.json` 和 `reference.png`. 回放校验原图哈希及冻结任务状态, 恢复原要求、初稿、探索报告、初稿问题和失败任务, 在新目录重新建立库; 不运行探索, 不带入旧整合决定、呈现记录或修复计数. 命令会调用配置的模型服务; `--max-main-calls` 必须为正数. 该入口用于相同输入下的有界对照, 不代表整条分析流程或视觉效果已经通过实测, 单次结果也不能证明稳定的成本下降.

初期固定样本 W1/W2/W3 仍可用独立实验入口复现, 其局部执行能力已由默认整合复用:

```sh
uv run --no-sync --env-file .env python -m shader_deep.experiments.controlled_replay \
  runs/real-business-budget-balls-20260922-54CxRg/run-avu0ozrm \
  --output-dir runs/controlled-dedup --max-main-calls 12 --max-work-calls 4 \
  --integration-max-output-tokens 65536
```

该入口复用相同冻结输入恢复器和 manifest, 仅处理固定真实 ID 的 W1 卡片阴影特征、W2 四个阴影候选和 W3 底场晕影特征. W2 依赖 W1 实际合并拥有者, 条件不满足时记录未执行原因; W3 独立, 剩余额度允许时继续. 总调用上限默认 12、每工作默认 4, 局部修复计入同一额度; 输出预算仍遵循原配置, 上述 65536 是本次实验的显式参数. 不更换模型、不重新探索、不自动扩大范围或重入暂缓工作.

普通工作只接收当前完整文本材料, 工具只允许本组同类型合并、保留或整组暂缓, 不开放原图测量、元素问题处置或任意引用更新. 特征工作比较对象范围及外观描述, 合并时保留全部候选与来源; 候选工作另行比较同一规范拥有者下的机制和必要前提. 候选列表不同或机制正文未提供本身不构成特征比较的暂缓理由, 也不能从候选 ID 推断未呈现的机制. 范围、外观或机制存在真实差异时仍保留, 当前判断材料不足时仍可暂缓.

模型判断仍由既有呈现、版本、范围与原子发布规则校验. `replay-manifest.json` 补充实际模型名、固定工作清单和预算; `controlled-summary.json` 分别记录三个步骤及其回执、未执行原因和最终库校验. 三步流程通过与语义检查分开记录, 不以一次接受或数量下降认定去重正确. 原失败探索任务和初稿问题继续影响整体 `partial`, 退出码也遵循整轮状态.

下面仅展示假想均匀背景的协议, 原图由调用方另外提供. 主任务提交:

```json
{
  "outline": {
    "elements": [{"id": "E1", "name": "背景", "scope": "整图", "salient_features": ["近似均匀的深色"]}],
    "relations": []
  },
  "directions": ["探索画面整体组成及底色机制", "独立检查局部色差与边界组织"]
}
```

一个子任务提交三库, 另一个任务独立提交自己的内容:

```json
{
  "sketch_library": [{"id": "S1", "name": "均匀底色", "element_ids": ["E1"], "composition": ["以统一颜色覆盖画面"]}],
  "feature_library": [],
  "relation_library": []
}
```

后端直接入库并提供轻量目录. 主 Agent 可读取一组组成:

```json
{
  "comparison": {"question": "检查两种底色组成是否重复", "target_ids": ["X1::S1", "X2::S1"]},
  "requests": [{"library": "sketch_library", "ids": ["X1::S1", "X2::S1"]}]
}
```

这里的 `X1`、`X2` 仅示意命名空间, 实际 ID 以目录为准. 下一请求呈现正文后, 无需修改时提交 `{"preserve": true}`, 有修改时提交相应变化字段. 没有剩余必要整合时调用 `finish_analysis` 请求检查并交付四库.

旧版协议保留在 [旧协议记录](../src/shader_deep/analysis/README.md) 作为历史记录. 当前实现依据[四库按需读取与批量整合方案](../../../documents/png-to-shader/分析模块优化方案-四库按需读取与批量整合-2026-09-21.md), 早期协议背景见[独立探索实施方案](../../../documents/png-to-shader/分析模块改造实施方案-独立探索与可能性库-2026-09-21.md).
