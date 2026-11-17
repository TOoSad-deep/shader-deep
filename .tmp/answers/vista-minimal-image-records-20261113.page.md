---
title: VISTA 最小图片记录设计
subtitle: 2026-11-13 已确认，暂不实施
lang: zh
template: doc
theme: paper
file: vista-minimal-image-records-20261113
---

VISTA 以“一张图片及其可选对应代码”为存储单位。后端负责保存，Agent 通过目录和读取工具回看。

状态：本轮设计已确认，代码尚未按本设计改造。本文是后续设计与实施的依据，不代表已有功能。

## A 定位与范围

VISTA 是可复用的视觉记忆模块。分析、规划、生成和调参 Agent 可以使用同一套存取能力。

| 责任 | 归属 |
| --- | --- |
| 保存 PNG、可选 GLSL 和对应 JSON | 后端接入与 VISTA |
| 扫描记录、读取实际图片和按需读取代码 | VISTA 工具 |
| 产生图片、提供实际代码与外部参数 | 对应业务后端 |
| 实现方式与 GLSL 结构的业务管理 | 业务模块 |
| 选择观察对象、判断、决定上下文保留时长 | Agent 与外层 harness |

首版不建立 index.json，不拆分通用材料记录。暂不引入材料 ID、refs、inputs、复杂派生关系或跨库设计。

## B 目录与记录单位

只有三个子文件夹，文件名由后端生成。

```tree
vista/
  glsl/
    shader-001.glsl
  png/
    reference-001.png
    render-001.png
    render-002.png
  json/
    reference-001.json
    render-001.json
    render-002.json
```

每份 JSON 指向一张 PNG，以及可选的一份 GLSL。不同 JSON 可以引用同一份 GLSL。

路径相对于 vista/ 根目录。JSON 文件名作为记录引用，不增加另一套 ID。

记录统一存放，来源 Agent 是 JSON 字段，不用于划分文件夹。

已经被记录引用的文件保持稳定。内容变化时保存新版本；已知代码没有变化时，可以复用原 GLSL 文件。

## C JSON 格式

基本字段为 label、png、glsl、source_agent。render 是可选字段。

```json title="渲染记录 · 已确认格式示例"
{
  "label": "第 2 次渲染",
  "png": "png/render-002.png",
  "glsl": "glsl/shader-001.glsl",
  "source_agent": "generation",
  "render": {
    "time": 0,
    "uniforms": {
      "radius": 0.3
    }
  }
}
```

参考图没有 GLSL。没有来源 Agent 时，source_agent 可以为 null。

```json title="参考图记录 · 已确认格式示例"
{
  "label": "参考图",
  "png": "png/reference-001.png",
  "glsl": null,
  "source_agent": null
}
```

名称、路径和来源由后端填写，模型不负责填写文件路径。

## D 外部渲染参数

render 保存本次实际使用、且没有包含在 GLSL 中的外部输入。由渲染后端提供，VISTA 保存和返回。

- 参数已经写在 GLSL 中时，不重复记录。
- 外部输入记录实际生效值，包括相关默认值，不只记录本次修改项。
- 参考图省略 render。
- 没有需要保存的外部输入时，也可以省略 render。

例如，两次渲染引用同一份 GLSL，但 radius 分别为 0.2 和 0.3。各自 JSON 保存自己的值，无需新增参数文件。

## E Agent 工具

首版只提供 list_records() 和 read_record()。保存由后端自动完成。

**list_records() 无参数。** 后端扫描已绑定的 vista/json/，返回简短目录。

```json title="list_records 返回示例"
{
  "records": [
    {
      "record": "render-002.json",
      "label": "第 2 次渲染",
      "source_agent": "generation",
      "has_glsl": true
    }
  ]
}
```

has_glsl 根据 glsl 是否为空生成，不新增存储字段。目录不展开图片或代码正文。

**read_record() 接收记录文件名和内容选择。** content 默认 image。

```json title="read_record 调用参数示例"
{
  "record": "render-002.json",
  "content": "both"
}
```

| content | 返回内容 |
| --- | --- |
| image | 实际图片 |
| code | 完整 GLSL 正文 |
| both | 图片与 GLSL |

读取结果附带 record、label、source_agent，以及存在时的 render。图片必须转换为模型可接收的图像内容，不能只返回路径。

Agent 只传 JSON 文件名。后端在绑定的 vista/json/ 中查找，并按记录解析文件。记录中的路径必须位于该 vista/ 内。

glsl 为 null 是正常情况，返回“没有关联代码”。请求 both 时仍返回图片。

所请求的文件缺失或无法读取时，明确报错，不替换成其他版本。参考图无代码与已登记代码文件丢失是两种不同情况。

## F 后端写入与发现

接收参考图时，先保存 PNG，再生成对应 JSON。glsl 为 null，省略 render，完成后返回记录摘要。

渲染成功时，后端使用本次实际代码、有效参数和结果图片完成登记。

```sequence num
业务后端 -> VISTA: 提供实际代码、参数与成功图片
VISTA -> 文件系统: 保存或复用确定版本的 GLSL
VISTA -> 文件系统: 保存本次 PNG
VISTA -> 文件系统: 完整写入并原子发布 JSON
VISTA --> 业务后端: 返回记录摘要
业务后端 --> Agent: 工具反馈包含 JSON 文件名与名称
```

新图片的工具反馈返回 record、label、source_agent 和 has_glsl。Agent 已知文件名时，可以直接读取，无需先列目录。

后端生成唯一文件名，不覆盖已发布记录。JSON 在内容文件保存完整后才发布；扫描不能返回半写入记录。

渲染失败、没有生成图片时，不创建 VISTA 图片记录。错误和失败代码继续由业务模块管理。

文件或 JSON 保存失败时，明确报告失败，不返回成功登记的记录。目录以已发布 JSON 为准。

## G 接入与验收

工具接入负责将读取结果送入实际模型请求。图片和代码在后续上下文中保留多久，由外层 harness 决定。

以下是后续实施的验收项，本轮未实施或执行这些行为测试。

- 参考图可列出和读取；读取代码时正常说明没有关联代码。
- 两份 JSON 可以引用同一份稳定 GLSL，并读取各自图片。
- 代码变化后产生新文件，旧记录仍读取原版本。
- 外部参数记录本次实际值，回读时随记录返回。
- image、code、both 三种读取符合请求范围。
- 实际模型请求包含图像内容与按需返回的代码。
- 新记录能从工具反馈直接发现，也能通过扫描目录发现。
- 未完成写入不出现在目录中，失败登记不返回成功。
- 所请求文件缺失时明确报错，不替换为其他版本。

## H 文档状态与下一步

本设计替代[2026-11-12 通用材料库草案](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep/docs/work-items/vista-visual-memory-module-proposal-20261112.md)。不继续沿用其中的集中材料索引、材料 ID 和通用关联设计。

[旧版最小 TODO](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep/docs/work-items/vista-minimal-harness-todo-20261109.md)和[旧版实施记录](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep/docs/work-items/vista-minimal-harness-implementation-20261111.md)保留为历史与现有实现依据。旧版验证结果不代表本设计已经实现或验证。

下一步可以依据本文编写实施方案。当前授权范围为设计整理，暂不改造生产代码。
