# 独立视觉评审工作流

[entry.py](entry.py) 的 `run_review` 接收参考 PNG、候选渲染 PNG、`ReviewRequest` 和可选 `ReviewOptions`。准备阶段捕获固定标准、共同规则、选定模板和实际图像。调用方提供 1:1 坐标窗口与必查矩形时，[覆盖预检](../../imaging/review_evidence.py)核对窗口尺寸，并检查两图的视图是否都包含该必查矩形。只有程序能据此确认**全部必查项**缺少所需视图时，才跳过模型并交付 `inconclusive`。部分缺视图或未配置坐标覆盖时，仍调用评审模型。坐标覆盖只回答“该位置有没有拍全”。例如中心孔的位置完整入图，孔是否实际存在仍须看图判断。

[session.py](session.py) 管理有限逻辑调用预算、接受提交、拒绝计数与终态。模型历史由 SDK 管理。合法提交通过领域校验后，先写入 `results/accepted.json`，成功后登记接受终态。正常结束但未提交报告为 `blocked`，保留 `no_submission` 原因。

[review_coverage.py](../../domain/review_coverage.py) 将程序未评项合并到提交，再核对状态与引用。一个维度同时存在可查项和未评项时，问题须用 `check` 指向仍可查的必查项，且由主维度直接引用。可见重大差异仍可得到 `needs_revision`，并随报告保留未评项。

显式指定 `protocol="visual_review_v2"` 时，[逐项合同](../../domain/review_items.py)为每条必查内容绑定 ID。程序拒绝范围外或已锁定的 ID；同一 ID 重放相同答案幂等，更改已登记答案会被拒绝。缺任何可答项时不能结束。每条答案保存依据、可见差异及可并存的未评原因，六维状态由程序汇总。全缺视图时模型答案为空，程序仍交付带有具体原因的正式报告。此协议保证必查项有记录，记录中的视觉判断仍需核对。

接受材料由[评审包存储](../../infrastructure/storage/review_package.py)发布并正式回读。报告包交付成功为 `completed`，预算耗尽或主动停止为 `blocked`，执行或发布错误为 `failed`。`needs_revision`、`inconclusive` 与 `no_major_issue_observed` 是单独的视觉结论。发布失败保留接受快照，并且不返回成功报告位置。

CLI 将输入文件读取与格式错误归为退出码 2. 运行目录创建、输入副本和结果写入故障归为退出码 1, 标准输出提供 JSON 失败索引.

[options.py](options.py) 管理预算和传输配置，[configuration.py](configuration.py) 读取默认 YAML 或调用方指定配置。逻辑调用默认最多 3 次，网络额外重试默认 2 次。输入和条件在本轮固定，更换标准或图片时创建新运行。

[工作流行为测试](../../../../tests/unit_tests/workflows/test_review.py)检查公开调用、交付、主动停止、图异常与发布失败。[实施记录](../../../../docs/work-items/independent-visual-review-implementation-20261117.md)维护进度，工程命令见[开发指南](../../../../docs/development.md)。
[评审证据回归](../../../../tests/unit_tests/workflows/test_review_evidence.py)从公开入口核对测量入模、视图不足和完整视图中的实际缺项，并正式回读报告。
