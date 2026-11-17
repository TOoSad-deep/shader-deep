# VISTA 视觉记忆：把材料留住，把注意力收回来 {#vista-overview}

首版已打通“保存、发现、回看、继续判断”。原始材料保存在运行目录，模型按需读取；单次真实对照尚未显示质量或效率收益。

- 程序自动登记输入、产物和裁剪，模型负责观察与选择。
- 默认只展开当前工作材料，旧图与代码可按 ID 找回。
- 回看内容进入下一次真实请求，随后收回为引用。
- 首版默认关闭，支持 PNG 与旧五库报告单方案。[](source:implementation)

@answer scope="2026-11-11 工作区快照 · 含未提交改动 · 单任务首版"
@meta locale=zh-CN style=editorial quality_profile=showcase
@source implementation title="实施记录：范围、验收与实测" path=apps/shader_deep/docs/work-items/vista-minimal-harness-implementation-20261111.md
@source adapter title="memory.py：业务映射、默认选材与临时回看" path=apps/shader_deep/src/shader_deep/agents/generation/memory.py
@source history title="history.py：请求副本与历史精简" path=apps/shader_deep/src/shader_deep/agents/generation/history.py
@source store title="visual_memory.py：索引、校验、目录和裁剪" path=apps/shader_deep/src/shader_deep/infrastructure/storage/visual_memory.py
@source loop title="middleware.py：预算与实际呈现资格" path=apps/shader_deep/src/shader_deep/agents/generation/middleware.py
@source options title="options.py：显式启用与独立模型预算" path=apps/shader_deep/src/shader_deep/agents/generation/options.py
@source trial title="真实模型对照：原始数据与失败记录" path=summary.json
@source probe title="受控协议探针：HTTP 图片与文件哈希匹配" path=protocol-evidence.json

## 01 · 各模块怎样协作？ {#architecture span=12}

@architecture id=system-map title="生成会话调度工具，公共存储只处理材料"
| entry | 输入与方案 | frontend |
| session | 生成会话 | backend |
| tools | 受限工具 | backend |
| request | 请求装配 | backend | source:history |
| renderer | WebGL 渲染 | backend |
| memory | 材料存储 | database | source:store |
| files | 运行目录 | database |
| model | 多模态模型 | external |
| entry > session | 固定输入 |
| session > tools | 执行动作 |
| session > request | 构造请求 |
| tools > renderer | 生成预览 |
| tools > memory | 登记与读取 |
| request > memory | 默认选材 |
| memory > files | 文件读写 |
| request > model | 文本与图像 | emphasis |

这是职责与访问关系图。模型返回后由会话继续调度，具体顺序见回看时序。

| 实现位置 | 负责什么 | 保持的边界 |
| --- | --- | --- |
| `memory.py` | 业务记录映射、默认选材、临时回看 | 候选与错误由生成侧解释 |
| `history.py` | 生成本轮请求副本，移出历史正文 | 原 SDK 消息和执行参数保持原样 |
| `visual_memory.py` | 编号、索引、哈希、回读、裁剪 | 不理解五库，不查询业务黑板 |

模块职责以当前工作区源码为依据。[](source:adapter)[](source:history)[](source:store)

## 02 · 保存全部，是否每轮都看全部？ {#context span=6}

**保存范围与请求内容分开控制。** 三层内容属于同一运行，不是三个独立记忆库。[](source:adapter)

| 内容层 | 包含什么 | 保留方式 |
| --- | --- | --- |
| 持久材料 | 输入副本、GLSL、PNG、结果 JSON、索引 | 文件继续保留 |
| 默认上下文 | 固定目标与方案、参考图、最新成功候选代码和整图 | 每轮提供 |
| 临时展开 | 主动回看、最近一次失败反馈 | 提供给紧接着的一轮 |

尚无新成功候选时，默认展示已有基线。最新候选只是工作版本，质量优劣仍需比较。

@callout level=note title="旧内容退出请求后仍然存在"
| 已归档 render 参数始终显示材料引用。代码正文只在默认材料或本轮回看中展开一份。 |
| 旧错误也收回为结果材料引用。需要时读取 JSON，保留原错误与候选关联。 |

## 03 · 材料放在哪里，怎样找到？ {#storage-layout span=6}

@tree id=run-tree title="一个运行目录，一份集中索引"
| run | run-xxx/ | - |
| inputs | inputs/ | run |
| code | candidate-001.glsl | run |
| preview | candidate-001.png | run |
| result | candidate-001-render.json | run |
| state | run.json | run |
| memory-dir | memory/ | run |
| index | index.json | memory-dir |
| derived | derived/ | memory-dir |

| 索引信息 | 用途 |
| --- | --- |
| `version`、`run_id`、`materials` | 标识索引版本、运行和材料集合 |
| 材料 ID | 作为 `materials` 的键，稳定引用 |
| `kind`、`label`、`path` | 确定类别、名称与相对文件位置 |
| `sha256`、`source` | 校验字节，追溯任务与操作 |
| `derived_from` | 裁剪来源与像素区域 |

`run.json` 持续更新业务状态，不作为固定哈希材料。不同候选即使图像相同，也保留各自来源。[](source:store)[](source:adapter)

## 04 · 一次回看，图片如何到达模型？ {#recall span=12}

@sequence id=recall-sequence title="回执留在历史，实际内容只展开一轮"
| actor model | 模型 | external |
| actor session | 生成会话 | backend | source:adapter |
| actor storage | 材料存储 | database | source:store |
| actor projection | 请求投影 | backend | source:history |
| phase | 请求回看 |
| model | session | read_materials：材料 ID 与可选区域 |
| session | storage | 校验路径、哈希与裁剪区域 |
| storage | session | 返回已校验的真实内容 | return |
| phase | 下一轮展开 |
| session | projection | 简短回执与临时内容 |
| projection | model | 有标签的实际图像或正文 | emphasis |
| model | session | 成功返回判断或下一步动作 | return |
| phase | 随后收回 |
| session | projection | 消费临时内容，保留读取引用 |
| projection | model | 后续请求只保留回执 |

@steps id=recall-guide
| recall-find | 模型 | 不知道 ID 时先列目录；知道 ID 可以直接读取。 | recall-sequence | model |
| recall-read | 材料存储 | 读取原件；需要裁剪时保存派生图和来源区域。 | recall-sequence | storage |
| recall-show | 请求投影 | 将实际内容加入 ToolMessage 副本，保留调用配对。 | recall-sequence | projection |
| recall-release | 生成会话 | 模型成功返回后释放临时内容；重试仍使用同一份内容。 | recall-sequence | session |

列目录只说明“材料存在”。实际看图需要图像进入请求。回读不会重新登记原件；新裁剪才产生新材料。[](source:adapter)

## 05 · 哪些条件满足后才能交付？ {#qualification span=12}

@workflow id=finish-flow title="登记、渲染、呈现、选择各有独立含义"
| rendered | 本轮渲染成功 | backend |
| full | 完整图进入请求 | backend |
| responded | 模型成功返回 | external |
| presented | 登记呈现资格 | security |
| finish | 校验并交付 | backend |
| rendered > full | 真实 PNG |
| full > responded | 实际呈现 |
| responded > presented | 提交资格 |
| presented > finish | 选择候选 | emphasis |

| 情况 | 能否获得新完成资格 |
| --- | --- |
| 只看到目录、代码或裁剪 | 不能 {bad:非完整预览} |
| 同批刚渲染就 finish | 不能 {caution:还没回读} |
| 首次整图请求成功返回 | 可以在该响应中选择 {good:已呈现} |
| 请求失败 | 不提交新资格 |
| 旧候选已看过整图，现已退出默认上下文 | 资格保留 |
| 外部输入基线 | 不作为本轮完成候选 |

请求内可选列表包含旧资格和本轮完整预览。最终读取已选代码，并核对代码与预览材料。程序完成仍不等于用户视觉验收。[](source:loop)

## 06 · 实际参考图 {#reference-image span=4}

@image path=assets/reference.png alt="实测输入：深灰背景上的青色圆" caption="自制 128 × 128 参考图"

这是验证用输入，不包含用户私人图像。两种模式使用同一参考图和渲染条件。[](source:trial)

## 07 · 原模式结果 {#baseline-image span=4}

@image path=assets/baseline.png alt="原模式最终渲染：圆的位置和大小接近参考" caption="真实模型生成，选中 candidate-002"

@facts
| 渲染 / 模型轮次 | 2 / 3 |
| 服务 token 合计 | 9,912 |
| 用时 | 22.283 秒 |

助手观察：圆的位置和大小较接近，颜色偏暗，边缘偏软。[](source:trial)

## 08 · 记忆模式结果 {#memory-image span=4}

@image path=assets/memory.png alt="记忆模式最终渲染：圆比参考偏大" caption="真实模型生成，首次编译失败后修复"

@facts
| 渲染 / 模型轮次 | 2 / 4 |
| 自主回看次数 | 1 |
| 服务 token 合计 / 用时 | 14,107 / 28.370 秒 |

助手观察：圆偏大且偏暗。本次结果不是用户视觉验收。[](source:trial)

## 09 · 测试通过，是否证明效果更好？ {#validation span=7}

@chart type=bar data=tokens.json x=mode y=tokens unit=token title="本次真实样例 token 合计；相同上限，不同执行路径"

@callout level=warning title="质量与成本收益尚未建立"
| 记忆模式增加了一轮模型调用，且首个候选编译失败。 |
| 两种模式预算相同，但不能把成本差异全部归因于记忆。 |

| 验证层次 | 已取得的证据 |
| --- | --- |
| 工作区回归快照 | 375 单元、22 集成通过；含并发规划任务 |
| VISTA 专项 | 存储 15、历史投影 8、生成闭环 8、初始化失败 1 |
| 真实模型对照 | 两种模式均完成，记忆模式自主回看一次 |
| 普遍收益 | 尚未建立 |

测试计数取自 2026-11-11 记录。本页面未重新运行测试。首次记忆测试曾遇到脚本客户端关闭问题；失败记录已保留。[](source:implementation)[](source:trial)

## 10 · 模型真的收到图片了吗？ {#wire-proof span=5}

@image path=assets/probe.png alt="实际通过工具响应发送的 64 × 64 裁剪图" caption="受控协议探针：来源区域 (32, 32, 64, 64)"

@facts
| 实际 HTTP 请求 | 1 次 |
| tool-role 图片块 | 1 个 |
| 图片字节与 SHA256 | 与落盘材料完全一致 |
| 服务结果 | 成功返回图像描述 |

这次受控探针验证了图像传输，不计入自主使用或对照成本。它只证明本次模型配置接受了该请求。[](source:probe)

## 11 · 如何使用，首版边界是什么？ {#usage span=12}

在 `apps/shader_deep/` 中执行，替换参考图路径和任务要求：

```bash title="显式启用视觉记忆"
uv run --no-sync --env-file .env shader-deep reference.png "复现参考图" --visual-memory --max-model-calls 12
```

| 项目 | 当前行为 |
| --- | --- |
| 默认状态 | 关闭；原调用方式保持兼容 |
| 可用入口 | PNG、既有生成任务、旧五库报告单方案 |
| 模型预算 | 开启时默认 12；显式值优先 |
| 单轮额外读取 | 最多 4 份，含失败和重复请求项 |
| 本轮未接入 | 新 planning/scene 输入、分析角色、调参子 Agent |
| 后续再验证 | 长任务收益、保留轮数、跨任务复用 |

公共配置新增有默认值的 keyword-only 参数。存储模块可复用，但其他业务流程不会自动获得记忆能力。[](source:options)[](source:implementation)

@callout level=note title="阅读本页的范围"
| 架构图展示的是本项目当前工作区实现，含未提交代码，不是 VISTA 论文规定的文件格式。 |
| 页面中的图片和数字来自本次运行记录；源码位置列在下方资料区。 |
