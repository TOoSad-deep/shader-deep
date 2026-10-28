"""参考图驱动的分析与 Shader 生成应用."""

# 从 api.py 进入工作流; workflows/ 装配角色和外部资源.
# agents/ 聚合原生分析与生成角色; domain/ 维护业务规则.
# infrastructure/ 适配网络与存储; imaging/ 和 rendering/ 执行确定性图像操作.
# context/ 与 tools/ 保留生成入口的导入转发.
# 包导入不启动模型、浏览器或文件写入.
