# 文件存储与输入捕获

## 职责

将已接受的业务对象发布为可校验文件, 读取报告或选定方案, 为规划与生成固定原图、观察、要求及方案. 本模块执行 IO 和文件格式校验. 观察与规划的任务推进分别由[观察工作流](../../workflows/analysis/README.md)和[规划工作流](../../workflows/planning/README.md)管理, 字段规则由各自领域合同定义.

## 阅读入口

| 路径 | 入口与用途 |
| --- | --- |
| [report_package.py](report_package.py) | `write_report_package` / `read_report_package` / `read_sketch`: 发布、整包校验和按方案取材 |
| [json_draft.py](json_draft.py) | `JsonDraftStore` 执行单份 JSON 草稿的版本核对、唯一替换及原子写入 |
| [observation_draft.py](observation_draft.py)、[planning_draft.py](planning_draft.py) | 分别将共用草稿绑定到观察与规划字段及业务诊断 |
| [observation_package.py](observation_package.py) | `write_observation_package` / `read_observation_package`: 全图 E/F/R 观察包发布与正式回读 |
| [planning_inputs.py](planning_inputs.py) | `capture_planning_source` 捕获观察、要求与生成约束, 计算完整观察身份 |
| [planning_package.py](planning_package.py) | `write_planning_package`、`read_planning_package`、`read_planning_sketch` 发布并校验整图 M/S 方案 |
| [review_package.py](review_package.py) | `capture_review_source` 固定双图与标准, `write_review_package` / `read_review_package` 发布和核验独立评审包 |
| [generation_planning_inputs.py](generation_planning_inputs.py) | `capture_planning_generation_inputs` 固定正式规划包、显式方案及本轮执行条件 |
| [analysis_materials.py](analysis_materials.py)、[report_evidence.py](report_evidence.py) | 冻结证据字节转为包内材料, 校验路径、哈希、像素与统计字段 |
| [generation_inputs.py](generation_inputs.py)、[scene_inputs.py](scene_inputs.py) | 捕获单方案或两元素来源, 形成后续请求使用的固定材料 |
| [generation_comparison.py](generation_comparison.py) | 比较索引及显式人工选择, 不代替子运行的候选选择 |
| [visual_memory.py](visual_memory.py) | `VisualMemory`: 集中材料索引、稳定 ID、目录分页、校验回读及原像素裁剪 |
| [artifacts.py](artifacts.py)、[snapshots.py](snapshots.py) | 创建独立运行目录, 写公开投影与单文件快照 |

## 输入、输出与状态

历史完整五库交付保存同一选定版本的五库、公共状态、固定原图和可选证据. `report/` 保存 manifest、五库 JSON、原图、导航及可选证据. `read_sketch` 返回显式选择的草图或一项局部备选所需材料.

轻量观察包独立使用 `scene_observations_v1`: manifest 保存状态、原图身份、上下文和问题, E/F/R 分为三个 JSON 文件, README 从同一快照派生全图树状视图. 保存合同见[观察领域](../../domain/scene_observations.py), `read_observation_package` 返回 `ObservationManifest` 与 `SceneObservations`. 观察包不包含 M/S, 不表示已具备草图生成输入.

整图规划包使用 `scene_planning_v1`, 包内保存固定观察副本、要求、生成约束、M/S 正文及派生候选索引. `read_planning_package` 返回 `PlanningPackage`, 其 source 提供固定观察与原图, report 提供规划正文. `read_planning_sketch` 根据 S 和可选备选序号返回生效选择、方法、组织说明及两类缺口. 领域规则见[scene_planning.py](../../domain/scene_planning.py).

生成输入捕获接收合法报告及明确选择, 输出副本、内容身份和内存材料. 后续请求复用捕获字节, 不跟随源文件的后续变化. 存储层不保存可续跑的模型会话, 业务终态由调用方决定.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 报告文件格式、导航或按草图读取 | `report_package.py`; 同步核对公开读取与生成消费者 |
| 全图观察包字段、主归属校验或树状导航 | `observation_package.py`; 与观察领域合同同步, 不改变旧完整五库读取语义 |
| 整图规划包、派生候选或方案读取 | `planning_package.py`, 对照 `scene_planning.py` 的覆盖、备选及缺口规则 |
| 规划源观察身份、原图及能力约束 | `planning_inputs.py`; 生成侧显式选择捕获在 `generation_planning_inputs.py` |
| 裁剪/统计证据的包内表达 | `analysis_materials.py`、`report_evidence.py`; 测量计算在 [imaging](../../imaging/measurements.py) |
| 固定输入、基线或来源组合 | `generation_inputs.py`、`scene_inputs.py`; 业务准备入口见[生成设计](../../../../docs/generation-architecture.md) |
| 视觉记忆材料的索引、回读和裁剪 | `visual_memory.py`; 任务输入与候选/错误映射由 [generation/memory.py](../../agents/generation/memory.py) 负责 |
| 调整哪个版本可以交付 | 转到 `AnalysisSession.finalize`, 不让文件写入决定业务成功 |

## 关键约定

- 报告在同级暂存目录写齐并用正式读取器复验后改名发布; 已发布目录不能原地覆盖. 单文件快照替换不等于多个文件事务.
- 证据按内容哈希命名并使用包内相对路径. 裁剪须与固定原图区域逐像素一致; 统计字段会校验聚合合同, 不声称从原图重新计算全部数值.
- 实际呈现后冻结的证据字节用于交付, 临时 PNG 被修改或清理不能悄悄替换已接受材料.
- 按草图读取保留整轮缺口, 按正文引用和关系端点补齐必要材料; 视图不是完整文件包, 局部备选必须显式选择.
- 比较中的人工选择只更新比较索引; 整图生成固定两个来源, 不将其旧候选登记为本轮生成候选.
- 历史完整五库、轻量观察和整图规划分别使用 `five_libraries_v1`、`scene_observations_v1`、`scene_planning_v1` 及各自正式读取器.
- 观察包保留原始短 ID 和提交顺序; 每个 F 必须恰有一个 E 主归属, R 只能引用本包 E/F, 问题可以引用 E/F/R. 纯背景包要求非空上下文. 所有必需文件的解析路径须位于包内, 原图字节须匹配哈希; partial 必须有 gaps, completed 不能有 gaps.

## 验证入口

[报告包](../../../../tests/unit_tests/infrastructure/test_report_package.py)、[证据](../../../../tests/unit_tests/infrastructure/test_report_evidence.py)、[生成输入](../../../../tests/unit_tests/infrastructure/test_generation_inputs.py)、[整图输入](../../../../tests/unit_tests/infrastructure/test_scene_inputs.py). 验证范围与维护见[开发指南](../../../../docs/development.md)和[同步约定](../../../../AGENTS.md#文档同步).

观察合同与发布验证见[领域测试](../../../../tests/unit_tests/domain/test_scene_observations.py)及[观察包测试](../../../../tests/unit_tests/infrastructure/test_observation_package.py), 覆盖多元素往返、非连续 ID、背景包、损坏引用/哈希/路径和失败暂存清理.


## 可编辑草稿

[JsonDraftStore](json_draft.py) 提供观察与规划共用的单文件草稿操作. [DraftStore](observation_draft.py) 固定到 run/draft/observations.json, [PlanningDraftStore](planning_draft.py) 固定到 run/draft/planning.json. create 只创建一次, 重复调用返回已有版本. read/report 检查实际字节 hash 与最近提交版本, 外部修改返回 external_draft_change. edit 要求请求版本一致、非空唯一匹配和 replace_all=false.

草稿沿用 SceneObservationSubmission 的字段结构, 允许暂存重号及悬空引用. [diagnose_scene_observations](../../domain/scene_observations.py) 返回结构路径、全部冲突位置和引用诊断, 正式接受继续使用完整领域校验. 修改后只校验字段结构, 保留原始编辑字节及其余内容的格式.

规划适配使用 ScenePlanningSubmission, 以当前固定观察调用 diagnose_scene_planning. 两个适配分别提供字段类型与诊断回调. SDK 后端、顺序批次和实际回读回执在[共用运行辅助](../../agents/draft_runtime.py).

编辑在共享 RLock 下先保存旧字节的内容哈希快照, 再完整写入唯一临时文件并原子替换当前草稿. 替换为提交点, 其前失败保留旧稿, 其后诊断失败保留新稿与新版本. 无内容变化不创建快照. 运行状态、回读前置条件与接受决定由调用方 session 管理.

[草稿回归测试](../../../../tests/unit_tests/infrastructure/test_observation_draft.py)覆盖内容保留、诊断定位、禁止覆盖、陈旧版本、非法 JSON、符号链接、快照/写入失败与提交点行为.

[规划草稿回归](../../../../tests/unit_tests/infrastructure/test_planning_draft.py)覆盖规划路径、字段省略往返、引用修复与分页实际入模. 规划包及生成交接的行为验证由[规划包测试](../../../../tests/unit_tests/infrastructure/test_planning_package.py)与[规划生成测试](../../../../tests/unit_tests/agents/test_planning_generation.py)维护.

## 规划输入与生成捕获

planning_inputs 捕获观察 manifest、E/F/R 与原图的实际字节, 用文件名和长度边界计算观察内容身份. 要求与 GenerationConstraints 作为本轮固定输入一起保存. 正式规划包将这些输入与 M/S 正文发布为自包含材料, 迁移目录后重新校验哈希、引用、完整性和候选派生.

generation_planning_inputs 捕获正式规划包, 校验副本内容身份, 按显式 S 和可选备选构造 CapturedPlanningInputs. 该对象为生成保存整图观察、生效方案、方法条件、上游观察 gaps 和 S.gaps. 省略生成配置时继承规划尺寸与时间, 显式配置的执行差异写入固定方案材料及结果 metadata.

方案来源与本轮执行条件在捕获阶段固定. 实际渲染候选及结束资格由[生成工作流](../../workflows/generation.py)与[生成会话](../../agents/generation/tools/session.py)管理.

## 独立视觉评审输入与报告

[review_package.py](review_package.py) 从显式参考与候选 PNG 捕获固定字节, 保存调用方任务、共同规则及选定层级模板. `CapturedReviewSource.images` 包含带角色标签的完整双图, 声明区域时增加来自对应原图的局部视图. 后续请求与正式发布采用同一份不可变字节集合. 原文件或输入副本被改变不会替换本轮已固定的材料.

捕获和正式回读均核对 PNG 签名、必需块、块长度、顺序与 CRC, 再解码并检查静态格式、32 MiB 文件容量和 16,777,216 像素上限. 1/2/4 位灰度局部图展开为 8 位灰度, 透明色键按同一比例换算. 区域使用原图左闭右开的像素坐标, 保留来源与固定区域. 正式回读重新验证局部视图与来源区域的像素一致性. 原图颜色与透明度保留, 呈现条件来自调用方声明.

16 位 PNG 的局部视图通过 [png16.py](../../imaging/png16.py) 按原始样本裁剪, 保留位深、色型、显式 alpha 与 `tRNS` 透明色键. 该处理支持灰度、RGB、灰度 alpha、RGBA 及 Adam7 交错编码, 输出同色型的非交错 PNG. 正式回读核对完整 16 位样本、透明键和颜色元数据. 解压上限按图像尺寸与各交错 pass 计算. 像素重建只处理裁剪所需的行前缀及预测依赖. 捕获时生成局部图, 发布时复用固定字节, 正式读取器独立核对磁盘中的局部图.

`visual_review_v1` 和 `visual_review_v2` 正式包都包含固定任务与标准、两张原图、实际比较视图、采用的规则和 `review.json`. `ReviewPackage.report` 保存提交、程序派生结论与执行元数据. 读取器核对协议文件集合、包内路径、哈希、固定绑定、领域引用和派生结论. 发布使用同级暂存目录, 写齐并正式回读后改名, 已有报告目录拒绝覆盖. 运行目录的接受快照与交付状态由评审工作流管理.

未指定协议的请求沿用 V1. V2 请求显式填写 `protocol="visual_review_v2"`, 采用 [items.md](../../resources/skills/visual-review/items.md) 和原有层级模板. 两版都将本次共同规则保存至 `rules/SKILL.md`. 发布和回读时, 清单版本、任务版本和规则头部协议必须一致. V2 的 `criteria.json` 保存每个 `check_id` 对应的维度和必查项原文. `review.json` 的 `check_answers` 用这些标识保存所有可评必查项的原始答案. 全部必查项均由程序确认缺少视图时保存空数组. V1 省略这两份清单. V2 读取器用固定标准、复算的覆盖证据和答案重建六维观察、问题及未评项, 拒绝与重建结果不一致的报告.

任务启用区域统计或覆盖时，[review_evidence.py](../../imaging/review_evidence.py) 从固定 PNG 与任务构造 `report.evidence`。正式读取器重新测量和检查覆盖，并核对报告内的程序未评记录。省略可选配置的旧任务继续使用相同的冻结标准摘要。

[评审包行为测试](../../../../tests/unit_tests/infrastructure/test_review_package.py)覆盖固定图片、源文件变化、局部像素、移动回读、内容篡改、路径与符号链接、派生结论及发布失败. 领域和视觉含义见[评审准则](../../../../docs/visual-review-rubric.md).

## 视觉材料存储

[VisualMemory](visual_memory.py) 将材料描述集中保存到本轮 `memory/index.json`, 原图、代码和结果正文继续使用实际文件. 稳定 ID 用于引用, SHA256 用于校验文件字节; 相同内容来自不同候选时保留各自来源. 路径相对于运行目录, 真实解析后必须仍在目录内. 回读缺失、变更或越界材料明确报错.

裁剪记录直接来源和像素区域, 保留原始 RGBA 像素, 相同任务/来源/区域可以复用. 索引通过同目录临时文件替换发布, 不承诺跨文件事务或模型会话恢复.

生成侧捕获允许的输入、登记候选文件, 并把单次渲染结果发布为固定 JSON 材料; 公共存储仅执行材料操作, 不查询黑板或解释错误. 持续更新的 `run.json` 不作为固定哈希材料. 模型回看工具及一轮展开策略在生成模块, 不由存储决定候选完成资格. 约定与验证项见[实施方案](../../../../docs/work-items/vista-minimal-harness-implementation-20261111.md), 存储行为测试见[test_visual_memory.py](../../../../tests/unit_tests/infrastructure/test_visual_memory.py).
