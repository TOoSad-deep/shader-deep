# 分析工作流

## 职责与入口

[entry.py](entry.py) 保持 run_analysis / run_analysis_task 公开入口, 固定输入并发布 scene_observations_v1 观察包. [pipeline.py](pipeline.py) 的 execute_scene_analysis 只调用一次原生观察图, 结束后根据合法快照核查交付. 不再建立方向、探索槽位、V0/V1 或整合角色.

[session.py](session.py) 的 AnalysisSession 仅保存固定要求、图像身份、有限调用预算、接受快照和终态. 返回合同为 [SceneAnalysisResult](contracts.py), 模型消息及 SDK 文件状态不复制到业务记录中.

[measurements.py](measurements.py) 将程序采集的原图区域与结果存为锚点。接受时的 `reference_measurements` 进入观察包 manifest，正式读取会核对图像身份与区域。没有测量的包省略该字段。定向复核使用 `AnalysisRevision` 捕获只读旧观察与争议区域，绑定同一原图，以新运行和新包保存结果。旧锚点随新包保留，同规格同结果沿用已有标识。每轮最多执行 6 个独立测量，包内累计最多保存 12 个锚点。

## 状态与交付

| 状态 | 含义 |
| --- | --- |
| running | 允许本图保存草稿、回读、局部编辑并完成 |
| accepted | 当前完整草稿已进入主请求, 通过领域校验且冻结快照保存成功 |
| stopped | 主动受阻、预算耗尽或没有合法提交而结束 |

模型普通文本不算报告. save 只创建磁盘草稿, edit 按请求版本局部原子修改, finish 冻结当前合法且已回读的版本. 草稿操作与接受由同一锁串行保护, 模型和网络 IO 在锁外. 终态后不能覆盖已接受观察, 图异常不会抹去已有合法快照.

合法提交无 gaps 时为 completed, 有必要观察缺口时为 partial; 无合法提交或发布失败为 failed. completed 不证明视觉拆分正确. 原图与新报告保留, 但不承诺恢复跨进程模型会话.

pipeline 在正常结束、异常和 KeyboardInterrupt 后统一 finalize. on_delivery 可返回发布后的最终 SceneAnalysisResult; 存在回调时由回调保存公开结果索引, pipeline 不覆盖它. 回调失败普通流程传播 IO, 中断流程先保留诊断再继续传播原中断.

finalize 只核查内存事实. pipeline 独立尝试 manifest 与 result 诊断文件, 单文件失败仍继续发布回调; 全盘失败保留原始 IO 和中断优先语义.

## 文件和配置

运行 manifest 保存实际单角色状态和逻辑调用计数, results/observations.json 保存接受提交与摊平观察; 最终 result.json / run.json 和 report/ 由入口交付形成. 同文件临时替换不等于多文件事务.

运行指标由程序记录: 安全模型标识、主规则/全部资源哈希、受控资源读取结果、合同错误和工具拒绝次数, 以及进入 pipeline 到交付前的 elapsed_seconds. 资源字段见[角色说明](../../agents/analysis/README.md). 使用 SDK UsageMetadataCallbackHandler 汇集实际报告的 token usage; 无数据为 null, 不推断缺失响应或网络重试成本, http_request_count 也保持未知. 不额外保存模型历史.

配置、弃用多角色字段和 CLI/YAML 迁移见[实施记录](../../../../docs/work-items/single-agent-observations-20261110.md). 观察角色仅使用通用模型传输值, 不借用旧 worker 阶段配置. 旧完整包读取继续由[文件存储](../../infrastructure/storage/README.md)维护.

## 验证

[原生角色行为](../../../../tests/unit_tests/agents/test_native_analysis.py)、[公开入口和失败交付](../../../../tests/unit_tests/workflows/test_five_public_api.py)、[配置加载](../../../../tests/unit_tests/workflows/test_analysis_config.py)共同验证输入、工具、终态和消费者合同. 真模型提交与五图粒度人工校准单独记录.


## 草稿状态

AnalysisSession 持有唯一 DraftStore, 正文在 draft/observations.json. draft_revision/request_revision/seen_revision 由程序管理, 终态后拒绝写入. 未 finish 时即使草稿字段合法, 仍按 failed 交付, execution 中保留 draft_path 与 revision 便于排查. 正式发布只采用 results/observations.json 对应的冻结对象. 详细合同见[草稿实施方案](../../../../docs/work-items/observation-draft-edit-plan-20261111.md).

分页回执识别 SDK 的 next offset 页头, 在同一版本下累计实际送入模型的行范围. 同批工具由角色按声明顺序执行, 连续编辑使用前一步提交版本. 回读与完成出现在同一响应时, 完成会提示下一轮调用, 待正文实际进入模型请求后再接受.

默认 max_model_calls 已按当前配置提高至20, CLI显式参数可覆盖该值. 历史8轮或12轮实验的预算保留在各自运行记录中.
