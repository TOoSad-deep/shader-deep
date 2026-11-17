# 分页与工具批次修复验证

代码已兼容 SDK 带 next offset 的分页页头, 同批工具按声明顺序串行执行, 同批编辑的版本随前一步提交推进. draft_unseen 反馈提供具体读取参数或下一轮完成提示.

280项单元测试、Ruff、格式、类型与仓库检查通过. 回归覆盖完整分页、同步/异步依赖编辑、save/read混合、同批read/finish和终态后拒绝写入.

| 尝试 | 状态 | 调用次数/上限 | 准入拒绝 | 证据 |
| --- | --- | --- | --- | --- |
| buffer-bloom | failed | 8/8 | 0 | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8b0-34b1-7470-8564-25f12277228f?poll=true) |
| auroras | completed | 5/8 | 0 | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8b0-33fa-73e3-8a99-ba853c4036ae?poll=true) · [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-tool-fix-20261111/auroras/run-og3q5b2c/report/README.md) |
| buffer-bloom-12 | completed | 5/12 | 0 | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8b9-d9dd-7540-a7e4-c8c645468639?poll=true) · [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-tool-fix-20261111/buffer-bloom-12/run-x7p1rhgm/report/README.md) |

极光在原8轮上限内, 5轮完成. 发光图首轮执行15次成功局部编辑, 8轮用尽时最后一次回读已返回, 尚缺后续完成调用. 该轮准入拒绝为0.

额外将发光图单次测试上限设为12, 实际5轮完成, 没有工具错误. 该次结果不能证明增加上限带来收益, 模型选择的修订路径有波动. 生产默认上限仍为8, 模型及思考参数沿用既有配置.

所有尝试与LangSmith轨迹保留, 两份成功正式包已通过读取器、原图哈希及当前/已见/接受版本一致性验证.

[原五图结果](../draft-edit-five-cases-20261111/README.md) · [程序指标](summary.json) · [8轮重测工具步骤](trace-steps.json)
