"""基于 Deep Agents 的 PNG 到 Shader 应用."""

# 源码入口导航:
# cli.py 解析命令行; agents/ 编排任务; context/ 准备模型材料;
# middleware.py 在调用时注入材料和检查预算; tools/ 执行动作;
# blackboard.py 管理业务记录; rendering/ 执行 WebGL2; artifacts.py 保存快照.
# analysis/ 保存多视角专用配置、报告校验和执行器; analysis_cli.py 提供独立入口.
# 包初始化不创建模型或启动浏览器, 实际运行从 agents/ 的公开函数开始.
