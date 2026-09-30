# 五库字段对照图

本图根据[首版字段基线](../analysis-five-libraries.md)绘制, 是已讨论设计的查阅视图, 不表示业务代码已经实现. 字段定义以该文档为准; 修改基线后同步更新图源并重新生成图片.

2026-09-29 用户已确认“完整五库包 + 短入口报告 + 按草图读取”的交付形式. 图中仍展示不变的逻辑字段和引用关系; 短入口与按草图读取不增加业务字段, 按草图读取是完整快照的读取视图. 公共字段的物理存放位置及文件布局见[数据设计第 8.8 节](../analysis-five-libraries.md#88-文件包入口与读取约定).

- [PNG 对照图](five-libraries-fields.png): 适合直接查看或分享.
- [SVG 矢量图](five-libraries-fields.svg): 可放大检查字段和连线.
- [Graphviz 图源](five-libraries-fields.dot): 保留可编辑字段表与引用连线.

图中 `?` 表示可选字段, `[]` 表示数组. 箭头表示数据引用, 不表示执行顺序. 蓝色连线表示候选机制, 紫色连线表示草图的实际选择及其目标. `alternatives` 内的缩进行是单个备选项的字段, 不是新的业务库.

在仓库根目录, 使用已安装的 Graphviz 和 librsvg 生成:

```sh
dot -Tsvg apps/shader_deep/docs/diagrams/five-libraries-fields.dot -o apps/shader_deep/docs/diagrams/five-libraries-fields.svg
rsvg-convert -w 1800 apps/shader_deep/docs/diagrams/five-libraries-fields.svg -o apps/shader_deep/docs/diagrams/five-libraries-fields.png
```

渲染环境需要中文字体; 本次使用 `PingFang SC`. 在受限环境中可仅为渲染命令指定可写的 `XDG_CACHE_HOME`, 不修改应用依赖.

2026-09-29 已生成并目视检查 PNG, 核对五库、本轮公共 state、格式元数据及嵌套字段. 完整图的每个节点是字段对照表, 不是已建数据库表或数据库迁移脚本.
