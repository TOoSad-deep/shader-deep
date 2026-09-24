# 开发与验证

所有命令在 `apps/shader_deep/` 执行. 当前入口与职责见 [architecture.md](architecture.md), 运行方法及预算见 [应用 README](../README.md).

设计取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md). 当前工作状态、验证证据和锁文件已知问题见[结构重构任务](work-items/structure-refactor.md). 新任务只为需要跨会话保留的工作建立记录, 不为同一状态维护多份进度文件.

## 修改一个角色

先看 `agents/<role>/` 的入口、提示词、上下文和工具. 业务数据及引用约束放入 `domain/`; 网络和文件细节放入 `infrastructure/`. 工作流负责创建共享资源和交付, 角色通过显式输入与回调使用它们.

- 修改语义判断: 找对应角色的提示词、提交契约和工具.
- 修改阶段顺序、并发或最终状态: 找 `workflows/`.
- 修改合并合法性、引用或版本规则: 找 `domain/library/`.
- 修改重试、调用额度或格式修复: 找 `runtime/` 与 `infrastructure/llm/`.
- 修改渲染与浏览器释放: 找 `rendering/` 和 `agents/generation/tools/`.

不从新实现导入旧路径转发模块. 已存在的公共函数保留签名, 新内部组件优先使用小类、函数和明确参数. `LibraryPersistence`、回放会话协议只服务实际的替换边界, 不要求每个类都有接口和工厂.

## 测试组织

`tests/unit_tests/` 按 `agents`、`workflows`、`domain`、`runtime`、`infrastructure`、`rendering`、`compatibility` 组织; `fixtures` 保存模型传输夹具. 已有行为断言保留, mock 位于搬迁后的实际依赖入口. 旧协议测试继续验证兼容实现, 不计作默认流程验证.

```sh
make check
# 以下按变更范围单独执行:
make test
make lint
make integration_test
```

`make test` 使用 unittest 并将警告作为错误. `make lint` 包括 Ruff、格式及 ty. 真实 WebGL2 检查使用模拟模型, 不消耗真实模型请求. 缺少环境或浏览器时先按 README 显式安装, 不修改依赖或关闭检查掩盖失败.

`make check` 顺序执行 `repository_check`、`test`、`lint`, 任一失败立即停止. 该命令不调用网络、浏览器或真实模型, 也不包含锁文件同步和打包; 它不代表全部验收条件已经通过.

`make repository_check` 检查应用 README、AGENTS、`docs/` 及 `src/shader_deep/` 下模块 README 中本地链接的目标路径, 以及 ADR 声明的直接导入边界. 它覆盖普通/相对/类型导入及绝对名称的字面量动态导入, 不执行业务模块. Markdown 检查覆盖行内链接及显式引用链接, 跳过代码示例; 不验证锚点、远端 URL、任意动态导入变量或传递依赖, 也不判断文档语义是否过时.

结束任务前, 在对应记录中更新剩余工作、已知失败和可复现验证. 无法证明的结果保留为未验证; 代码与记录一起进入版本控制后, 新工作树才可获取同一份知识.

## 打包和入口

```sh
uv build --out-dir /private/tmp/shader-deep-dist
uv run --no-sync python scripts/check_distribution.py /private/tmp/shader-deep-dist/shader_deep_app-0.1.0-py3-none-any.whl
uv lock --check
uv run --no-sync python -m shader_deep.cli.analysis --help
uv run --no-sync python -m shader_deep.cli.replay --help
uv run --no-sync python main.py --help
```

默认 YAML 位于包内 `resources/analysis.yaml`, WebGL2 脚本位于 `rendering/webgl2.js`. 修改资源位置时同步维护 `pyproject.toml` 的 package-data, 并从构建出的 wheel 检查资源、旧入口与新入口. 仅在可编辑安装中运行成功不足以证明安装包完整.

上面的 wheel 文件名以实际构建输出为准. `check_distribution.py` 只用于本仓库构建的包, 在临时解包目录执行导入及 CLI 帮助检查, 使用当前环境已有依赖, 不安装包或调用真实模型.

禁止读取真实 `.env` 来运行普通测试. 真实模型评估与图片视觉验收需要单独的明确范围, 不由代码结构重构自动触发.
