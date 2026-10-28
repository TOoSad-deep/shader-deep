# 文件存储与输入捕获

## 职责

将已接受的业务对象发布为可校验文件, 读取完整报告或选定草图, 为生成固定报告、原图和基线输入. 本模块执行 IO 和文件格式校验; 任务推进与版本选择在[工作流](../../workflows/analysis/README.md), 五库对象规则在[领域层](../../domain/five_libraries/README.md).

## 阅读入口

| 路径 | 入口与用途 |
| --- | --- |
| [report_package.py](report_package.py) | `write_report_package` / `read_report_package` / `read_sketch`: 发布、整包校验和按方案取材 |
| [analysis_materials.py](analysis_materials.py)、[report_evidence.py](report_evidence.py) | 冻结证据字节转为包内材料, 校验路径、哈希、像素与统计字段 |
| [generation_inputs.py](generation_inputs.py)、[scene_inputs.py](scene_inputs.py) | 捕获单方案或两元素来源, 形成后续请求使用的固定材料 |
| [generation_comparison.py](generation_comparison.py) | 比较索引及显式人工选择, 不代替子运行的候选选择 |
| [artifacts.py](artifacts.py)、[snapshots.py](snapshots.py) | 创建独立运行目录, 写公开投影与单文件快照 |

## 输入、输出与状态

分析交付接受同一选定版本的五库、公共状态、固定原图和可选证据. `report/` 保存 manifest、五库 JSON、原图、导航及可选证据; `read_sketch` 返回指定草图/局部备选所需的视图, 不改原包.

生成输入捕获接收合法报告及明确选择, 输出副本、内容身份和内存材料. 后续请求复用捕获字节, 不跟随源文件的后续变化. 存储层不保存可续跑的模型会话, 业务终态由调用方决定.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 报告文件格式、导航或按草图读取 | `report_package.py`; 同步核对公开读取与生成消费者 |
| 裁剪/统计证据的包内表达 | `analysis_materials.py`、`report_evidence.py`; 测量计算在 [imaging](../../imaging/measurements.py) |
| 固定输入、基线或来源组合 | `generation_inputs.py`、`scene_inputs.py`; 业务准备入口见[生成设计](../../../../docs/generation-architecture.md) |
| 调整哪个版本可以交付 | 转到 `AnalysisSession.finalize`, 不让文件写入决定业务成功 |

## 关键约定

- 报告在同级暂存目录写齐并用正式读取器复验后改名发布; 已发布目录不能原地覆盖. 单文件快照替换不等于多个文件事务.
- 证据按内容哈希命名并使用包内相对路径. 裁剪须与固定原图区域逐像素一致; 统计字段会校验聚合合同, 不声称从原图重新计算全部数值.
- 实际呈现后冻结的证据字节用于交付, 临时 PNG 被修改或清理不能悄悄替换已接受材料.
- 按草图读取保留整轮缺口, 按正文引用和关系端点补齐必要材料; 视图不是完整文件包, 局部备选必须显式选择.
- 比较中的人工选择只更新比较索引; 整图生成固定两个来源, 不将其旧候选登记为本轮生成候选.
- 当前读写协议为 `five_libraries_v1`; 保存着旧四库文件不代表此读取器支持自动转换或续跑旧任务.

## 验证入口

[报告包](../../../../tests/unit_tests/infrastructure/test_report_package.py)、[证据](../../../../tests/unit_tests/infrastructure/test_report_evidence.py)、[生成输入](../../../../tests/unit_tests/infrastructure/test_generation_inputs.py)、[整图输入](../../../../tests/unit_tests/infrastructure/test_scene_inputs.py). 验证范围与维护见[开发指南](../../../../docs/development.md)和[同步约定](../../../../AGENTS.md#文档同步).
