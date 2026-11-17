# 单个全图规划 Agent 实施校准

模型为 z-ai/glm-5.3-flash。初始使用提供方默认推理，后续补测显式发送 low。规划总调用预算为 20。

每个运行记录实际规则哈希和输出上限。初版为 163,840，后续默认收口至 32,768。不同版本与预算分别记录。

当前正式归档 10 / 10 个唯一 case。

## 规划尝试

| 尝试 | 状态 | 逻辑调用 | M | S | 备选 | 规则 | effort | 输出上限 | 时间秒 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [2d-physics-balls](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/2d-physics-balls/planning/run-5qpnc_et/report/README.md) | completed | 5 | 5 | 1 | 1 | R0 | provider | 163840 | 855.6 |
| auroras | failed | 1 |  |  |  | R1 | provider | 32768 | 333.9 |
| [auroras-low](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/auroras-low/planning/run-ti45jjxp/report/README.md) | partial | 7 | 9 | 1 | 0 | R1 | low | 32768 | 74.7 |
| [buffer-bloom](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/planning/run-lpy1hj6e/report/README.md) | completed | 6 | 7 | 1 | 2 | R0 | provider | 163840 | 133.3 |
| [buffer-bloom-r1](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/planning/run-io2iricx/report/README.md) | completed | 4 | 8 | 1 | 0 | R1 | provider | 163840 | 219.4 |
| cool-s-distance | failed | 4 |  |  |  | R1 | provider | 163840 | 1661.0 |
| [cool-s-distance-low](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/cool-s-distance-low/planning/run-s13id8xz/report/README.md) | completed | 19 | 5 | 2 | 2 | R1 | low | 32768 | 1328.5 |
| cool-s-distance-retry | failed | 1 |  |  |  | R1 | provider | 32768 | 366.1 |
| dna-helis | failed | 2 |  |  |  | R1 | provider | 163840 | 624.2 |
| [dna-helis-low](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/dna-helis-low/planning/run-mkt14462/report/README.md) | completed | 5 | 7 | 1 | 0 | R1 | low | 32768 | 102.1 |
| dna-helis-retry | failed | 1 |  |  |  | R1 | provider | 32768 | 244.7 |
| [flat-water-effects2](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/flat-water-effects2/planning/run-3tqu967t/report/README.md) | completed | 3 | 9 | 1 | 1 | R1 | provider | 163840 | 222.5 |
| [liquid-glass](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/liquid-glass/planning/run-41g6qbc_/report/README.md) | completed | 6 | 7 | 1 | 2 | R0 | provider | 163840 | 396.6 |
| [pink-circle](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/pink-circle/planning/run-gbztz1xn/report/README.md) | completed | 6 | 8 | 1 | 1 | R0 | provider | 163840 | 251.5 |
| [supah-frosted-glass](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/supah-frosted-glass/planning/run-xvxngequ/report/README.md) | completed | 6 | 6 | 1 | 1 | R0 | provider | 163840 | 647.2 |
| windows-95 | failed | 4 |  |  |  | R0 | provider | 163840 | 1813.4 |
| [windows-95-low](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/windows-95-low/planning/run-_anfk734/report/README.md) | partial | 15 | 5 | 1 | 0 | R1 | low | 32768 | 502.2 |
| windows-95-retry | failed | 7 |  |  |  | R1 | provider | 32768 | 1444.8 |

## 生成尝试

| 来源 | 尝试 | 状态 | 渲染次数 | 模型调用 | 代码 | 预览 |
| --- | --- | --- | --- | --- | --- | --- |
| buffer-bloom | generation | completed | 3 | 4 | [GLSL](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/generation/run-zwm_06kv/candidate-003.glsl) | [PNG](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/generation/run-zwm_06kv/candidate-003.png) |
| buffer-bloom-r1 | generation | blocked | 3 | 4 | 未选定 | 无选中预览 |
| buffer-bloom-r1 | generation_retry | completed | 5 | 6 | [GLSL](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-005.glsl) | [PNG](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-005.png) |
| supah-frosted-glass | generation | completed | 2 | 3 | [GLSL](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/supah-frosted-glass/generation/run-o7a9z_64/candidate-002.glsl) | [PNG](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/supah-frosted-glass/generation/run-o7a9z_64/candidate-002.png) |

## 全部实际产出 PNG

生成验证覆盖发光图与磨砂叠圆两个 case，共产出 10 张 PNG，包含 3 张最终选中项和 7 张中间候选。尺寸均为 1280 × 720。

### 发光图初版

运行 `run-zwm_06kv`。

#### candidate-001 · 中间候选

![发光图初版 candidate-001，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/generation/run-zwm_06kv/candidate-001.png)

#### candidate-002 · 中间候选

![发光图初版 candidate-002，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/generation/run-zwm_06kv/candidate-002.png)

#### candidate-003 · 最终选中

![发光图初版 candidate-003，最终选中](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom/generation/run-zwm_06kv/candidate-003.png)

### 发光图修订版首次生成

运行 `run-9scv802b`。

该次运行的 3 次 GLSL 编译失败记录保存在运行目录，结束状态为 blocked。

### 发光图修订版生成重试

运行 `run-5a7u1293`。

#### candidate-001 · 中间候选

![发光图修订版生成重试 candidate-001，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-001.png)

#### candidate-002 · 中间候选

![发光图修订版生成重试 candidate-002，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-002.png)

#### candidate-003 · 中间候选

![发光图修订版生成重试 candidate-003，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-003.png)

#### candidate-004 · 中间候选

![发光图修订版生成重试 candidate-004，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-004.png)

#### candidate-005 · 最终选中

![发光图修订版生成重试 candidate-005，最终选中](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/buffer-bloom-r1/generation-retry/run-5a7u1293/candidate-005.png)

### 磨砂叠圆

运行 `run-o7a9z_64`。

#### candidate-001 · 中间候选

![磨砂叠圆 candidate-001，中间候选](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/supah-frosted-glass/generation/run-o7a9z_64/candidate-001.png)

#### candidate-002 · 最终选中

![磨砂叠圆 candidate-002，最终选中](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/scene-planning-implementation-20261111/supah-frosted-glass/generation/run-o7a9z_64/candidate-002.png)

## 运行事实与已知偏差

- 正式读取、版本相符及预览尺寸由程序检查。主要语义与视觉结论另经原图对照。
- Windows 原始尝试因长请求由校准 harness 中断，草稿及失败索引保留，单独以较低输出上限重试。
- 首次发光图出现备选 serializer 警告，已修复 Python/JSON 模式处理并补回归。
- 初版发光图有星形与心形距离函数、坐标原点导致的形状及裁切问题。修订规划清除了跨 M 身份依赖，仍有图像规格耦合。
- 磨砂原观察误将前圆写成比背景亮，并扩大边缘柔化。规划继承了误读，生成又改动位置，预览与原图有明显差异。
- 修订版发光图首次生成因三次 GLSL 编译错误以 blocked 结束。独立生成重试临时采用 6 次渲染预算，原失败保留。
- M 自包含仍不稳定。Windows、Auroras、DNA 保留跨方法引用，Cool-S 的真实备选仍有正文独立性与方法条件冲突。详见 artifact-review.md。
- Windows 与 Auroras 的 partial gaps 混入数值拟合，必要性仍需语义校准。原始状态保留。
- 既有 uv.lock 的 SDK 为 0.7.13，HEAD SDK 为 0.7.14。当前工程验证使用已安装环境。

## LangSmith

- [2d-physics-balls](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c931-5677-7813-900b-933970ff1fe5?poll=true)
- [auroras](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c957-e6b9-7283-adf2-556e687b46c3?poll=true)
- [auroras-low](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c960-c388-7b30-8d35-0dd21f9dd03b?poll=true)
- [buffer-bloom](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c926-518e-7fb3-8cfe-4dc4fd6f2425?poll=true)
- [buffer-bloom-r1](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c939-078b-72b3-a4ee-a919e1ea9ebc?poll=true)
- [cool-s-distance](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c93e-827d-78e3-8167-a97de982128e?poll=true)
- [cool-s-distance-low](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c960-aa7d-7703-ad5b-2c2c35fc41ef?poll=true)
- [cool-s-distance-retry](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c957-ee68-70e1-b65c-eb57317ef151?poll=true)
- [dna-helis](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c94e-549d-7cd1-982f-4704e0840a38?poll=true)
- [dna-helis-low](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c95d-007e-7af3-bd34-45245da4f520?poll=true)
- [dna-helis-retry](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c958-0959-76f1-81aa-511c64776cbd?poll=true)
- [flat-water-effects2](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c94a-d825-73c2-b9ff-c602c7df22fa?poll=true)
- [liquid-glass](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c92b-2be8-7c03-9221-3aef6911f70b?poll=true)
- [pink-circle](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c92b-2be8-78c0-863d-443ed1622f7d?poll=true)
- [supah-frosted-glass](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c92a-1996-7140-9b79-a8fe2e5e0c84?poll=true)
- [windows-95](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c92f-2194-7822-9d4d-72b70e33cfc3?poll=true)
- [windows-95-low](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c960-982e-7942-b394-0b93137ddfdf?poll=true)
- [windows-95-retry](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c94a-733b-7562-8ad2-ad0ec531e870?poll=true)
- [发光图生成重试](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c945-f498-7673-878a-c7e7ea9da295?poll=true)

轨迹核对见 [trace-verification.json](trace-verification.json)。细分索引保留模型、资源、token usage、全部旧版草稿及源报告身份。

## 工程证据

[单元与静态检查](verification/check.log) / [浏览器集成](verification/integration.log) / [安装包检查](verification/wheel.log) / [包与源码一致性](verification/wheel-source-match.json) / [环境与默认值](verification/environment.json)

[内容复核](artifact-review.md) / [机器汇总](summary.json)
