# 项目文档

按主题归档。每个主题的正文和 HTML 入口放在该目录顶层，配套图、生成脚本与辅助数据分别存放。

## 问题与待优化项

- [问题与优化记录索引](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/documents/问题与优化/README.md)：集中记录已发现的问题、证据、优先级、待优化方向和处理状态。

## PNG → Shader 架构与流程

- [阅读 Markdown（正文与 Mermaid）](png-to-shader/分层生成流程.md)
- [打开 HTML](png-to-shader/index.html)
- [共享黑板与 Context Builder 设计草案 V0.2](png-to-shader/Context-Builder-v0.2.md)
- [独立多视角分析：已实现的数据结构、执行关系与源码导航](png-to-shader/多视角分析.md)
- [当前源码结构、阅读顺序与审查记录（2026-09-11）](png-to-shader/源码结构与审查-2026-09-11.md)
- [分析模块改造实施方案：独立探索与可能性库（2026-09-21，草案）](png-to-shader/分析模块改造实施方案-独立探索与可能性库-2026-09-21.md)
- [分析模块优化方案：四库按需读取与批量整合（2026-09-21，待实施）](png-to-shader/分析模块优化方案-四库按需读取与批量整合-2026-09-21.md)

```text
png-to-shader/
├── 分层生成流程.md       # 正文来源，优先修改此文件
├── Context-Builder-v0.2.md # 当前草案：共享黑板与上下文构造
├── Context-Builder-v0.1.md # 历史草案
├── 源码结构与审查-2026-09-11.md # 实现审查、源码导航和验证记录
├── index.html           # 生成的阅读页面
├── diagrams/            # SVG 图与独立 Mermaid 文件
├── scripts/build.py     # 从 Markdown 生成 HTML、图与数据
└── generated/           # 流程数据与核对记录
```

修改分层生成流程正文后，在项目根目录运行：

```sh
python3 documents/png-to-shader/scripts/build.py
```

生成脚本使用 Python 标准库和已安装的 Graphviz `dot`。`diagrams/` 和 `generated/flow-content.json` 由脚本生成；`generated/verification.json` 记录最近一次文件核对，不是持续有效的测试状态。
