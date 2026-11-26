# 独立视觉评审工作流

[entry.py](entry.py) 的 `run_review` 接收参考 PNG、候选渲染 PNG、`ReviewRequest` 和可选 `ReviewOptions`。准备阶段捕获固定标准、共同规则、选定模板和实际图像。调用方提供 1:1 坐标窗口与必查矩形时，[覆盖预检](../../imaging/review_evidence.py)核对窗口尺寸，并检查两图的视图是否都包含该必查矩形。只有程序能据此确认**全部必查项**缺少所需视图时，才跳过模型并交付 `inconclusive`。部分缺视图或未配置坐标覆盖时，仍调用评审模型。坐标覆盖只回答“该位置有没有拍全”。例如中心孔的位置完整入图，孔是否实际存在仍须看图判断。

整体验证由 `freeze_workflow_review` 在门禁 A 绑定 V2 规则、模板和各必查项的 `source_ref`，随后 `run_workflow_review` 使用这份冻结标准。两图采用同原点完整画布，或用 `coverage` 明确指定 1:1 窗口。工作流把 region_stats 锚点和优化器目标、保护区映射到各自原图，仅对两图完全覆盖的区域复算成对统计。crop 锚点仅表示已查看的位置。16 位 PNG 在匹配完整透明键后取各通道的高 8 位，再计算白底合成的编码颜色和亮度。参考锚点须来自当前 PNG。整条工作流可以在同一对输入上移除额外的局部裁剪并复评，底层实际采集窗口按原始 PNG 大小核验。人工决策后换帧可以记录候选实际时间。

工作流报告包同时保存 `gate-a-criteria.json` 和本轮有效的标准、采样与来源。正式回读会核对必查项、模板、覆盖条件及原有采样，允许在冻结版本上仅补充测量、扩大视图和更新候选来源或时间。送模时在图像外增加 `REFERENCE` 和 `CANDIDATE` 角色栏。装饰后的全图和局部图沿用原 PNG 的颜色空间及显示元数据，灰度图保持灰度色型。测量、裁图和报告校验均使用原始字节。

[session.py](session.py) 管理有限逻辑调用预算、接受提交、拒绝计数与终态。模型历史由 SDK 管理。合法提交通过领域校验后，先写入 `results/accepted.json`，成功后登记接受终态。正常结束但未提交报告为 `blocked`，保留 `no_submission` 原因。

[review_coverage.py](../../domain/review_coverage.py) 将程序未评项合并到提交，再核对状态与引用。一个维度同时存在可查项和未评项时，问题须用 `check` 指向仍可查的必查项，且由主维度直接引用。可见重大差异仍可得到 `needs_revision`，并随报告保留未评项。

显式指定 `protocol="visual_review_v2"` 时，[逐项合同](../../domain/review_items.py)为每条必查内容绑定 ID。程序拒绝范围外或已锁定的 ID；同一 ID 重放相同答案幂等，用普通登记工具覆盖已有答案会被拒绝。单项确需改判时，使用显式重评工具提交当前版本、理由和证据。结束时，程序把 `finish_review.answers` 和已有答复放在一起校验。重复、遗漏、未知、已锁定的 ID 或最终快照写入失败，都会保留之前的进度；成功后完整答案以接受快照为准。[角色工具与审计](../../agents/review/README.md)

每条答案保存依据、可见差异及可并存的未评原因，六维状态由程序汇总。全缺视图时模型答案为空，程序仍交付带有具体原因的正式报告。此协议保证必查项有记录，记录中的视觉判断仍需核对。

接受材料由[评审包存储](../../infrastructure/storage/review_package.py)发布并正式回读。报告包交付成功为 `completed`，预算耗尽或主动停止为 `blocked`，执行或发布错误为 `failed`。`needs_revision`、`inconclusive` 与 `no_major_issue_observed` 是单独的视觉结论。发布失败保留接受快照，并且不返回成功报告位置。

V2 的发布以及同一运行的恢复入口共用[发布工作流](publication.py)。已接受的运行可以从原运行目录恢复，核对已存在的报告或重新写出缺失的正式包，并补齐运行索引。尚未接受的运行、缺恢复依据的历史运行及身份冲突的报告会给出错误。报告交付后，公开索引同时提供重大问题、必要未评项及轻微问题数量。

V2 已注册工具的参数或领域校验被拒时，运行目录的 `results/last_rejection.json` 保存最近一次拒绝的工具名、轮次、错误代码、字段位置及参数结构。参数 schema 能定位错误时，另存 `schema_error_type`，可区分同一路径下的字段缺失与类型不符。主动停止或无提交且没有拒绝时，运行记录保留停止原因，诊断文件可以不存在。

CLI 将输入文件读取与格式错误归为退出码 2. 运行目录创建、输入副本和结果写入故障归为退出码 1, 标准输出提供 JSON 失败索引.

[options.py](options.py) 管理预算和传输配置，[configuration.py](configuration.py) 读取默认 YAML 或调用方指定配置。逻辑调用默认最多 3 次，网络额外重试默认 2 次。输入和条件在本轮固定，更换标准或图片时创建新运行。

[工作流行为测试](../../../../tests/unit_tests/workflows/test_review.py)检查公开调用、交付、主动停止、图异常与发布失败。[实施记录](../../../../docs/work-items/independent-visual-review-implementation-20261117.md)维护进度，工程命令见[开发指南](../../../../docs/development.md)。
[评审证据回归](../../../../tests/unit_tests/workflows/test_review_evidence.py)从公开入口核对测量入模、视图不足和完整视图中的实际缺项，并正式回读报告。

工作流 V2 报告按未评项保存 `evidence_gaps`。程序核对共享画布、覆盖与成对采样，已有像素可补采时登记 `insufficient_crop_or_sample`；缺帧、缺上下文或位置无法核实时登记 `missing_context_or_frame`。同一候选的补采保留首次报告，冻结标准与原始 PNG 身份继续沿用。
