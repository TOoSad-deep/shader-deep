# 历史四库类型与校验

本目录仅保留历史四库的数据模型与无 IO 校验, 供已保存结果的类型识别及现有数据引用使用. [models.py](models.py)定义历史报告与四库对象, [validation.py](validation.py)检查其结构和引用. 它们不启动分析、整合、发布或回放.

旧 LibraryStore、材料呈现门禁、合并执行与版本持久化已删除. 历史运行和报告文件保留原样; 四库数据不会自动转换或被五库报告读取器接受.

当前五库模型与引用规则在 `domain/five_libraries/`, 阅读入口见[当前架构](../../../../docs/architecture.md). 删除边界见[清理记录](../../../../docs/work-items/analysis-legacy-cleanup-20261028.md).
