# Shader Deep 应用开发约定

## 适用范围与规则来源

本文件适用于 `apps/shader_deep/` 及其子目录。通用开发、公共接口、安全、Git 和 PR 规范继续遵循[根目录 AGENTS.md](../../AGENTS.md)；本文件补充应用层约定。

**本目录的代码注释、文档字符串和维护文档使用中文，覆盖根目录中对应的美式英语要求。** 标识符、协议字段和文档解析格式按下文保留。

先阅读本次任务涉及的源码和测试。运行方式见 [README.md](README.md)，命令以 [Makefile](Makefile) 为准，依赖和检查配置以 [pyproject.toml](pyproject.toml) 为准。设计文档用于理解背景；实现状态以当前源码和验证结果为准。

## 知识与任务入口

先根据本次改动读取对应模块说明, 再沿其中链接定位源码和必要测试. 跨模块关系看[架构总览](docs/architecture.md), 命令与验证范围看[开发指南](docs/development.md).

| 本次改动 | 优先读取 |
| --- | --- |
| 单观察角色、工具、skill、上下文或权限 | [分析角色](src/shader_deep/agents/analysis/README.md) |
| 固定输入、接受快照、终态、失败交付或运行参数 | [分析工作流](src/shader_deep/workflows/analysis/README.md) |
| 全图 E/F/R 字段、归属和结构引用 | [观察合同](src/shader_deep/domain/scene_observations.py)、[实施记录](docs/work-items/single-agent-observations-20261110.md) |
| 单规划角色、工具、规则、输入或终态 | [规划角色](src/shader_deep/agents/planning/README.md)、[规划工作流](src/shader_deep/workflows/planning/README.md) |
| 新 M/S、备选、两类 gaps 或规划包 | [规划合同](src/shader_deep/domain/scene_planning.py)、[规划包](src/shader_deep/infrastructure/storage/planning_package.py)、[业务设计](docs/work-items/five-library-planning-contract-20261111.md) |
| 五库字段、引用或合并含义 | [五库领域](src/shader_deep/domain/five_libraries/README.md); 详细业务定义查[数据设计](docs/analysis-five-libraries.md) |
| 模型配置、输出预算、原始工具参数或网络传输 | [模型适配](src/shader_deep/infrastructure/llm/README.md) |
| 报告包、证据文件、生成输入捕获或保存格式 | [文件存储](src/shader_deep/infrastructure/storage/README.md) |
| 生成、渲染、视觉记忆、两方案比较或两元素整图组合 | [生成架构](docs/generation-architecture.md); 从本次涉及的执行分支进入 |

模块 README 说明当前实现, ADR 记录仍有效的设计取舍, `docs/work-items/` 保存对应任务的计划、进度与验证证据. 查历史演变时再读[原生切换记录](docs/work-items/deep-agents-native-plan-20261028.md)、[旧执行清理](docs/work-items/analysis-legacy-cleanup-20261028.md)或相关生成记录; 已完成计划不覆盖当前源码与约定.

## 文档同步

- 修改模块前阅读对应 README. 职责、公开接口、输入输出、状态归属、依赖关系或关键约束改变时, 在同一轮改动中更新模块说明; 普通内部实现调整无需机械更新文档.
- 新增或移动模块时补阅读入口和上层导航; 删除实现时清理旧入口与失效链接. 简单辅助目录可由上层说明覆盖, 不强制每个文件夹新增 README.
- 每项事实保留一个主要说明位置: 模块细节在相邻 README, 跨模块关系在架构总览, 字段合同在数据设计与源码, 具体算法在代码注释. 其他位置链接引用, 不复制维护.
- 跨会话工作更新对应任务记录, 区分计划、已实现、验证证据与未覆盖范围; 小改动不另建一套进度文件. 历史验证不能代替当前状态检查.
- 文档变更运行 `make repository_check` 并检查 diff. 链接与依赖检查不能证明文字符合代码, 交付前仍须对照涉及的入口与约束.

## 应用范围

- 当前提供三个独立入口。`shader-deep-analyze` 交付全图 E/F/R，`shader-deep-plan` 用一个原生角色组织整图 M/S，`shader-deep` 执行生成、渲染与候选选择。
- 规划消费固定观察版本，调用方通过明确的 S 与可选一项备选接入整图生成。独立视觉评审和持续决策按具体任务设计。
- 优先完成需求范围内的最小改动。应用问题先在本包解决；涉及 SDK 或其他包时，再读取对应目录的约定并说明跨包修改原因。
- 优先使用 Deep Agents 现有能力. 新增模块或自定义框架机制前, 简要说明已有模块/框架无法满足的具体需求, 避免为假设中的瓶颈增加通用抽象.
- 保留现有公开入口及兼容包装，包括 `main.py`、`generate_shader`、`generate_task`、`run_shader`、`run_generation`、`run_analysis` 和 `run_analysis_task`。签名变更遵循根目录的兼容要求。
- 新规划公开入口为 `run_planning`、`read_planning_package`、`read_planning_sketch` 和 `run_generation_from_planning`。整图方案与旧单元素报告使用各自明确的绑定和捕获类型。

## 源码职责与查找范围

以下路径均相对于本目录：

| 路径 | 职责 |
| --- | --- |
| `src/shader_deep/api.py`、`cli/` | 稳定调用入口、参数、输出与退出码 |
| `src/shader_deep/workflows/` | 运行创建、固定业务事实、单次原生图调用与最终交付 |
| `src/shader_deep/agents/` | 单观察、单规划与生成角色的提示词、上下文及受限工具 |
| `src/shader_deep/domain/` | 任务、黑板、证据和五库业务规则; 历史类型仅用于读取与校验; 不依赖模型框架或 IO |
| `src/shader_deep/infrastructure/` | 模型客户端、网络恢复、配置读取、存储和追踪 |
| `src/shader_deep/imaging/` | 固定参考图、裁剪、统计与剖面 |
| `src/shader_deep/rendering/` | 独立 WebGL2 渲染器及浏览器端脚本 |
| `src/shader_deep/resources/` | 随安装包发布的默认 YAML |
| `tests/unit_tests/`、`tests/integration_tests/` | 按职责组织的无网络业务测试、真实浏览器集成测试 |

当前调用链和状态归属见 [docs/architecture.md](docs/architecture.md), 验证与打包见 [docs/development.md](docs/development.md).

使用 `rg` 时优先限定到相关模块和测试。默认排除 `.venv/`、`__pycache__/`、`*.egg-info/`、`runs/` 和生成制品；需要诊断某次运行时，只读取对应运行目录。不要为应用层问题无差别扫描整个上游仓库。

## 中文注释与文档

- 注释解释模块职责、调用顺序、设计原因、状态变化和边界条件。重点说明并发提交、预算计数、重试条件、证据来源及资源释放；避免逐行复述直观代码。
- 公共函数继续使用 Google 风格文档字符串。保留 `Args:`、`Returns:`、`Raises:`、`Attributes:` 等解析标题，内容使用中文；参数名与签名一致，类型写在签名中。
- Python 注释和文档字符串沿用现有半角标点风格，例如 `,`、`;`、`:`、`.`，以符合 Ruff 的标点和文档字符串检查。不要为了中文注释全局关闭检查规则。
- 保留类名、函数名、参数名、JSON 键、工具名、环境变量名、状态枚举和技术术语。翻译注释时不顺带重命名这些接口。
- 提示词、命令行帮助、错误消息和测试样例字符串属于运行内容。修改这些内容应作为明确的行为或文案变更处理，不混入纯注释翻译。
- `@tool` 函数以及被工具包装的方法，其文档字符串会成为模型可见的工具说明；Pydantic 类型说明也可能进入工具结构描述。翻译时保留参数含义、前置条件和限制，并检查相关工具链路。
- 注释与代码不符时，以实际实现为依据修正文档；不声称未执行的检查已经通过，也不将历史联调结果泛化到所有模型服务。

## 业务状态、上下文与制品边界

- 黑板承载目标、任务、候选、结果及可选测量记录。PNG 和 GLSL 等制品内容由文件承载；模型历史、客户端、浏览器、锁、线程池及运行计数由各自的会话层管理。
- 黑板登记函数校验引用并返回新状态，调用方必须接收返回值。不得用相同 ID 覆盖旧记录，或通过直接修改共享嵌套字典绕过登记规则。
- 任务的 `target_version`、`baseline_id` 和显式输入引用在登记后保持固定。采用新目标或基线时创建新任务；新候选的产生和选择不自动改写旧任务。
- `read_task` 只解析业务引用。记录存在于黑板中，不表示它已经被发送给模型；Context Builder 负责加载任务实际需要的代码、图像和证据。
- 固定图像与业务材料通过请求级注入保持可用, 避免重复追加到持久消息. 原生分析的历史、todos、文件和摘要交给 SDK, 应用业务记录不保存第二份模型历史. 生成继续保持工具调用/响应配对.
- 观察角色持续接收固定整图与要求, 详细规则和样例按需读取. 修改消息装配时验证实际多模态请求, 不能只检查文件路径或文本占位符.
- 制品按独立 `run-*` 目录保存，失败尝试也应可追溯。快照通过同目录临时文件替换写入；不要将其描述为多个文件的事务或可恢复完整模型会话的检查点。
- 区分候选已登记、渲染成功、材料已进入模型请求、模型提交结果、用户视觉验收。`completed` 和 `presented` 等程序状态不证明视觉效果已被用户接受。

## 生成与渲染约定

- 生成默认仅开放 `render_shader`、`finish_shader` 和 `stop_generation`; `visual_memory=True` 时增加 `list_materials`、`read_materials`. 可见工具与执行白名单同步限制, 记忆模式首版限 PNG/既有任务及旧五库报告单方案, planning/scene 输入拒绝开启. 材料与请求选材职责见[生成架构](docs/generation-architecture.md#按需视觉记忆).
- `stop_generation` 记录非空受阻原因、允许引用的候选和补充输入/另试方案建议, 返回 `blocked`, 不自动派发任务或选择候选. 同批工具按顺序执行, 终态后的工具不能重开会话.
- 渲染宽度、高度、时间和预算由程序固定。渲染失败也消耗尝试次数；模型调用次数单独计数，避免纯文本回复或无效调用形成无限循环。
- 生成关闭自动模型摘要. `max_model_calls` 显式值优先, 省略时原模式为 `max_attempts + 2`, 记忆模式为 12; 图步数、剩余额度和运行记录使用同一解析值, SDK 有限网络重试不计入逻辑轮数. 报告模式未指定的宽高取自本轮固定原图, 旧 PNG 模式默认值保持不变.
- `finish_shader` 只能选择本次会话成功渲染，且预览已进入后续模型请求的候选。同一批调用刚渲染出的候选不能跳过回读立即结束。
- 最终交付读取已选候选的实际 GLSL 文件，保留自检说明及差异；不能用模型最后另写的一段未经渲染代码替代它。
- `rendering/` 保持独立，不导入 Agent、黑板或 LangChain。Python 管理浏览器与参数，`webgl2.js` 负责真实编译、链接、绘制和 PNG 导出。
- 渲染输入保持单 Pass GLSL ES 3.00 `mainImage` 契约。版本、precision、`iResolution`、`iTime` 和 `main` 由渲染器提供；修改契约时同步更新工具说明与测试。
- Playwright 同步对象在所属线程创建、使用和关闭。生成会话使用单工作线程串行执行渲染与选择；成功、失败和退出路径都必须释放浏览器、线程及 GPU 对象。
- 保留失败阶段与驱动原始日志。直接导出画布 PNG，不能用页面截图、静默缩放或其他处理替代后继续声称渲染条件相同。

## 单 Agent 观察、规划与历史报告约定

- 默认分析采用一个原生 Deep Agents 观察角色. 工具为 save_observation_draft、read_file、edit_file、finish_analysis 和 stop_analysis. 草稿仅位于 /draft/observations.json, 可见工具和实际执行白名单均需验证.
- F 在 E 下提交, 程序派生 feature_ids. R 可以涉及同元素或跨元素的 E/F. 首次保存完整草稿, 后续通过唯一匹配编辑局部内容. 字段结构在写入前校验, 跨条目引用问题可暂存并反馈具体位置, finish 接受时执行完整校验.
- 草稿以磁盘字节为唯一可编辑来源. 同批工具按声明顺序串行执行, 每次编辑基于前一步提交版本, 终态后拒绝后续操作. 完成前必须完整回读最新版本并实际送入后续模型请求. 接受后的结束钩子位于 SDK 摘要和计数之前. 运行状态见[分析工作流](src/shader_deep/workflows/analysis/README.md).
- scene_observations_v1 观察包使用独立读取入口. five_libraries_v1 及其证据、草图读取继续保留, 历史类型不构成多角色执行支持. 观察完成不等于 M/S 规划或视觉验收完成.
- scene_planning_v1 绑定完整观察内容身份、用户要求和实际生成约束。规划只维护 mechanisms、sketches 与可选 extra_candidates，候选索引由程序派生。
- 规划工具为 save_planning_draft、read_file、edit_file、finish_planning 和 stop_planning，唯一可编辑路径为 /draft/planning.json。主 skill 提供共同流程，复杂方法、备选迁移和条件缺口按[规划角色导航](src/shader_deep/agents/planning/README.md)读取参考。
- 生成材料保留所选方案条件、上游观察 gaps 与 S.gaps。覆盖与引用由程序检查，方法成立条件和组合语义通过模型复核及实际生成核对。
- 新整图生成显式选择 S 和可选一项零基备选。默认继承规划尺寸与时间，显式覆盖固定为本轮执行条件并记录差异。
- 参考图、快照与报告保留来源和实际字节; 文件或引用合法不证明视觉结论正确. 报告发布失败不能登记成功结果. 新运行不承诺跨进程模型会话恢复.

## 配置、输出与追踪

- 模型配置由 `infrastructure/llm/client.py` 按 `OPENROUTER_*`、`DS_MICU_*`、`MICU_*` 顺序统一选择: 首个任一项非空的组须完整配置, 全空占位组跳过. 不得跨组补齐模型、地址或密钥. OpenRouter 分析的推理块回传与输出参数规则位于 `infrastructure/llm/openrouter.py` 和 `transport.py`.
- 应用读取进程环境变量，`.env` 由启动命令加载。保留用户既有配置；凭据不写入源码、测试、快照或提交记录。模型 ID 更新时按根目录要求核验官方资料。
- 生成 CLI 的标准输出承载最终 GLSL。观察与规划 CLI 的标准输出承载状态和文件包位置的 JSON 索引，正文由各自文件包承载。诊断和制品位置写入标准错误。
- 保留运行、任务及父子任务的追踪关联。启用 LangSmith 会传输模型和工具输入中的图像、代码及报告；沿用用户的追踪配置，不在普通测试中启用真实外部追踪。

## 开发与验证

以下命令在 `apps/shader_deep/` 内执行：

| 命令 | 用途 |
| --- | --- |
| `make sync` | 按锁文件显式安装应用及开发依赖 |
| `make browsers` | 安装与当前 Playwright 匹配的 Chromium |
| `make test` | 使用标准库 unittest 运行无网络业务测试，警告按错误处理 |
| `make lint` | 检查 Ruff 规则、格式和 ty 类型 |
| `make format` | 格式化应用 Python 文件 |
| `make type` | 单独检查应用类型 |
| `make repository_check` | 离线检查当前文档本地链接和关键导入边界 |
| `make check` | 顺序执行仓库检查、单元测试和 lint |
| `make integration_test` | 先做最小浏览器检查，通过后运行真实 WebGL2 集成测试 |

- 本包使用 `unittest`，不照搬上游 pytest 命令或新增测试依赖。常规检查通过 `uv run --no-sync` 使用已安装环境；缺少依赖时先明确执行 `make sync`。
- 业务逻辑、工具契约或模型可见说明变更，执行 `make test` 和 `make lint`；新增测试应保护实际行为，不只断言实现结构或中文文案。
- 纯维护文档变更核对内容、路径和命令即可。纯注释变更检查可执行逻辑未被改动并运行 `make lint`；涉及工具文档字符串时按模型可见说明变更处理。
- 修改渲染器、浏览器生命周期或图像反馈链路时，补跑 `make integration_test`。模拟模型测试不证明真实服务可用；真实浏览器测试不证明模型视觉判断或用户验收通过。
- 浏览器出现 `kill EPERM`、`SIGABRT` 或初始化失败时，先定位权限与环境原因，不在同一失败环境反复启动；不要关闭 Chromium 沙箱或系统崩溃提示来掩盖问题。
- 若受限环境中的 uv 缓存不可写，可为当前命令设置 `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache`，不修改全局环境或依赖版本。
- 测试失败时保留完整日志，区分本次引入的问题与修改前可复现的问题；不通过删除断言、扩大警告过滤或报告部分成功来隐藏失败。不把某次测试计数或暂时性失败固化为长期基线。

## 交付与文档维护

- 开始修改前检查工作区状态，保留用户已有的未提交和未跟踪文件。提交时仅暂存本任务的明确路径，沿用根目录的 Git 与 PR 规范。
- 对外行为、工具契约、目录或运行方式变化时，同步更新本包 README 及必要的维护文档。架构背景见[多视角分析](../../documents/png-to-shader/多视角分析.md)和[源码结构与审查](../../documents/png-to-shader/源码结构与审查-2026-09-11.md)，其中历史内容需对照当前源码。
- 完成说明使用中文，交代修改内容、验证结果和实际未解决项。未运行的检查明确标注；设计、实现、实测效果与用户验收分别说明。
