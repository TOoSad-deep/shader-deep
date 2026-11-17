# 五图草稿工具回归结果

使用同一冻结版本、z-ai/glm-5.3-flash 模型和 8 次主模型调用上限, 并发 3. LangSmith 项目为 shader-deep, 筛选标签为 draft-edit-five-cases-20261111.

结果为 3 completed、2 failed. 五条轨迹均已远程核验, 三份正式报告通过读取器、原图哈希与已见/接受版本一致性检查.

| Case | 状态 | 调用次数 | 成功局部编辑 | 结果与轨迹 |
| --- | --- | --- | --- | --- |
| cool-s-distance | completed | 7/8 | 3 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-five-cases-20261111/cool-s-distance/run-_sle9rke/report/README.md) · [LangSmith](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c87a-e1d7-7023-a0df-9a4818c66103?poll=true) |
| flat-water-effects2 | completed | 6/8 | 1 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-five-cases-20261111/flat-water-effects2/run-p78wrn5t/report/README.md) · [LangSmith](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c87a-e298-7670-814c-54c8b50966eb?poll=true) |
| dna-helis | completed | 6/8 | 1 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-five-cases-20261111/dna-helis/run-ylqg7ahv/report/README.md) · [LangSmith](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c87a-deff-78b0-a3ba-73b38a99e840?poll=true) |
| buffer-bloom | failed | 8/8 | 2 | [草稿](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-five-cases-20261111/buffer-bloom/run-5v4kx2kp/draft/observations.json) · [LangSmith](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c87c-73b4-7e03-8680-af28b96b6860?poll=true) |
| auroras | failed | 8/8 | 2 | [草稿](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-five-cases-20261111/auroras/run-0fdxcyfu/draft/observations.json) · [LangSmith](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c87e-359b-75e3-aa02-a61a1519b197?poll=true) |

## 未完成原因

- buffer-bloom 在完成阶段收到 draft_unseen, 并有五次工具准入拒绝. 轨迹反馈包含写入/完成必须独占响应的批次冲突. 八轮用尽后停止, 草稿保留.
- auroras 在完成阶段两次收到 draft_unseen. 八轮用尽后停止, 草稿保留.
- 两例均没有被登记为成功报告. 本轮没有增加预算或改动运行源码与 skill.

## 诊断入口

- [运行指标](summary.json)
- [实际工具步骤](trace-steps.json)
- [失败反馈与 finish 错误](failure-details.json)

本轮评估工具流程完成情况. completed 的观察语义仍需结合原图审阅.
