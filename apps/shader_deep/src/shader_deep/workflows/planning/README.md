# 单个全图规划工作流

[entry.py](entry.py) 的 run_planning 接收 scene_observations_v1 包、本次要求、PlanningOptions 和可选 GenerationOptions。准备阶段捕获固定输入，创建一个原生规划会话并发布 scene_planning_v1。

[session.py](session.py) 管理草稿版本、主模型调用预算、最新完整已见回执、接受快照和终态。模型历史、摘要及会话内文件由 SDK 管理。

finish_planning 只接受已完整回读且正式领域校验合法的最新草稿。accepted 状态在结果快照写入成功后登记。预算耗尽、主动停止和模型普通文字结束会保留草稿与原因。

至少一套 S 没有上游必要观察缺口与本方案 gaps 时，正式发布为 completed。其余合法接受规划为 partial。正式包发布失败时公开结果为 failed，已有接受正文继续保留。

运行目录保存 inputs、draft、results、report、manifest.json 和 result.json。新正式包由[规划包存储](../../infrastructure/storage/planning_package.py)发布与读取。字段和语义见[业务设计稿](../../../../docs/work-items/five-library-planning-contract-20261111.md)。

[级联范围](cascade.py) 从新旧观察中的归属、参与关系与锚点内容计算受影响目标。传入 `revision` 时预填旧稿，接受时保护其他目标使用的机制、默认和备选中的选择、缺口与局部试验。移除只涉及受影响目标的备选时，其余备选可顺延。全图组合说明按新观察与实际机制选择重新校验。

规划暴露可选 `observation_revision_request`，同时将其保存到终态诊断中。传入 `revision_feedback` 可在新运行中提供前一次方法或局部试验的证据，用户要求仍由原参数传递。

[review_criteria.py](review_criteria.py) 只用固定参考 PNG、观察、上下文和要求生成整图标准草案。模型须填写六个具名维度。每个维度提交至少一条带来源的检查，或说明该维度不适用的具体原因。每条检查可附原图矩形和已有容差，程序将其编译为同一份整图标准。观察修订时从已有草案或人工冻结的标准中，仅替换受变更来源影响的检查。门禁确认后冻结 V2 标准。局部试验从冻结标准按来源派生对应必查项，并通过 [LocalProbeBinding](../../domain/local_probe.py) 固定生效方案、机制、上下文、原图坐标与预算。

`run_planning_generation_comparison` 并行执行两项不同的方案选择。两项共用固定包身份和渲染配置，可附同一份冻结标准和支持并行调用的生成模型。整图对照可复用与两项方案均兼容的已验证片段，局部对照在固定上下文中只替换方案和对应机制。每项保留自己的生成与盲评目录，评审从固定观察包获取参考锚点，并使用各自的调参区域。比较 JSON 和 README 供人对照。见[并行对照入口](../generation_comparison.py)。

[options.py](options.py) 提供有限调用和传输配置，[configuration.py](configuration.py) 读取包内 planning.yaml 或显式 YAML。CLI 显式参数覆盖 YAML，YAML 的相对输出目录基于配置文件所在目录。

规划默认最多 20 次主模型调用，每次请求输出上限为 32,768 token。`--max-output-tokens` 可显式覆盖该上限。推理 effort 沿用模型服务配置，未指定时采用提供方默认。该输出上限控制单次请求的资源规模，实际消耗以 SDK 返回的 usage 为准。

OpenRouter 的多数提供方将推理 token 与可见正文共同计入请求输出预算。Chat Completions 使用 `max_tokens` 表达这一预算，推理占满时可能返回 `finish_reason="length"`。详见 [OpenRouter 推理 token 文档](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。

`request_timeout_seconds` 控制连接、读取、写入和连接池等待等传输操作的超时。活跃 SSE 可以持续返回数据，因此完整响应的墙钟时间可以超过该值。

验证通过 API、真实 SDK 图与无网络模型执行保存、局部修订、回读、接受及失败交付。工程命令见[开发指南](../../../../docs/development.md)。
