# 本地可视化界面

## 范围与实现

2026-10-23: 新增 `make web` / `python -m shader_deep.cli.web`, 在本机提供 PNG 上传、直接生成、多视角分析、从本会话报告中显式选择草图及局部备选再生成. 显示日志、终态、候选预览、报告与文件下载, 支持停止任务. 使用 Python 标准库 HTTP、已有 Pillow/Pydantic 与原生 HTML/CSS/JS, 未新增依赖或修改既有公共签名.

`web/jobs.py` 通过固定 Python 模块与参数列表运行现有 CLI, 继承启动环境. 任务独立保存于 `runs/web/web-*/`, 页面不接触 API 密钥. 同时最多执行一个任务, 通过进程组终止回收子进程. HTTP 仅监听回环地址, 校验 Host、Origin 和页面令牌; 输入图片与数值有上限, 制品下载拒绝越界和外部符号链接.

页面服务重启不恢复历史索引或模型会话; 落盘结果仍保留. 分析预算沿用既有 YAML, 不擅自修改默认值. 报告模式采用页面显式尺寸. 部分报告可供查看和显式选择; 预算耗尽、错误、受阻、已停止保持独立状态, 候选存在不等于最终选择. 本轮不接入旧运行导入、多方案比较和整图组合.

## 验证

验证对象为本轮未提交工作区, 保留原有 `generation-02-execution-loop.md` 修改及其他未跟踪文件.

- `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check`: 558 项单元测试通过, 文档/导入检查 0 项问题, Ruff 规则、格式及 ty 类型检查通过.
- `uv run --no-sync python -W error -m unittest tests.integration_tests.test_web -v`: 3 项真实本机 HTTP 测试通过, 覆盖上传/轮询/下载、来源与令牌校验、路径越界与无效请求. 子进程使用固定替身, 不调用模型.
- 浏览器实际交互: 上传 PNG 并预览、切换模式、直接生成任务提交、部分完成报告、草图及局部备选选择、报告到生成、取消、预算耗尽时仅提供候选下载、刷新保留服务会话均已观察. 交互运行使用 `/private/tmp` 下的临时测试服务和固定样例, 没有将其当作真实生成效果; 该夹具不随产品交付.
- `node --check src/shader_deep/web/assets/app.js`: 语法检查通过.
- `uv build --offline --out-dir /private/tmp/shader-deep-web-dist` 与 `scripts/check_distribution.py`: 安装包资源、导入隔离和 7 个 CLI 帮助入口通过. HTML/CSS/JS 已纳入 package-data.

受限环境的默认 uv 缓存曾报 `Operation not permitted`, 常规检查改用临时缓存后通过; 离线构建使用获准访问的既有缓存, 未新增项目依赖. 本机 HTTP 测试使用允许绑定回环端口的执行权限.

本轮没有读取 `.env` 发起真实模型测试, 没有重跑未修改的 WebGL2 渲染集成套件; 浏览器任务结果来自固定测试夹具. 真实提供方可用性、模型生成效果和用户视觉验收仍需用户提交实际任务验证. 测试服务在交付前关闭, 使用 `make web` 启动正式界面.
