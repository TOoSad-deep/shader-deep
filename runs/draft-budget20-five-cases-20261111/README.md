# 20轮预算五图重测

默认主模型调用上限已提高为20, Python默认值、YAML和CLI帮助已同步. make check通过280项单元测试及Ruff、格式、ty和仓库检查.

本轮五张图最终均completed. 共六次尝试, 极光首轮在第2次调用发生连接错误, 独立重试完成. 所有尝试的LangSmith轨迹均已远程核验.

LangSmith项目为shader-deep, 标签为draft-budget20-five-cases-20261111. 调用次数以下方运行manifest中的计数为准.

| Case | 最终状态 | 实际调用/上限 | 证据 |
| --- | --- | --- | --- |
| cool-s-distance | completed | 8/20 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-budget20-five-cases-20261111/cool-s-distance/run-z2sdk4es/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c0-60f9-70d1-b37b-881c2bb65f49?poll=true) |
| flat-water-effects2 | completed | 5/20 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-budget20-five-cases-20261111/flat-water-effects2/run-1f07vxci/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c0-60e3-7751-beb1-6a7c75c1f8cd?poll=true) |
| dna-helis | completed | 12/20 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-budget20-five-cases-20261111/dna-helis/run-5sdvy11h/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c0-60ed-7320-97a0-bdee3af374c3?poll=true) |
| buffer-bloom | completed | 8/20 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-budget20-five-cases-20261111/buffer-bloom/run-dr3mzom2/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c1-703d-7220-803b-10fc174664d4?poll=true) |
| auroras（重试） | completed | 7/20 | [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-budget20-five-cases-20261111/auroras-retry/run-u65vafwx/report/README.md) · [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c4-c2bf-7a80-b77b-d43502bc5930?poll=true) |

[极光首次连接失败轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c8c3-8a21-7172-be5c-d744364b3194?poll=true)

五份正式包通过读取器、原图哈希与当前/已见/接受版本一致性检查. 本轮未对全部报告重新进行视觉质量打分.

[完整程序指标](summary.json) · [冻结实验配置](experiment.json)
