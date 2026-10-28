# 开发与验证

所有命令在 `apps/shader_deep/` 执行. 当前入口与职责见 [architecture.md](architecture.md), 运行方法及预算见 [应用 README](../README.md).

设计取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md). 跨会话任务使用对应的 `docs/work-items/` 记录; 历史检查结果不代表当前工作区已通过验证.

## 开始一次改动

1. 核对 Git 状态, 保留已有改动, 明确本次目标和完成条件.
2. 在[模块导航](architecture.md#模块导航)找到相关 README, 阅读职责、状态归属和常见修改入口; 需要时再展开数据设计或历史记录.
3. 优先在既有模块及框架能力内实现. 跨模块调整时先明确各模块责任; 使用子 agent 时为文件或职责分配互不重叠的所有者.
4. 根据[文档同步约定](../AGENTS.md#文档同步)判断说明是否需要同步. 更改职责、接口或关键约束时更新, 内部细节调整不强制改文档.
5. 执行与改动相关的验证, 留下结果及未覆盖范围. 发现已替代实现或旧链接时一并收口, 不保持两套相互矛盾的当前说明.

公开函数和核心参数按应用约定保留; 框架用法以当前安装源码为准. 测试命令以 [Makefile](../Makefile) 为准, 模块 README 只链接相关行为测试与需要注意的验证范围.

## 测试组织

`tests/unit_tests/` 按角色、工作流、领域、传输/存储和渲染职责组织; `fixtures` 保存模型传输夹具. 保留当前可观察行为的断言, mock 对准实际依赖入口. 已退出执行器的专属回归随实现删除, 不让旧协议成为原生流程要求.

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
uv run --no-sync python main.py --help
```

默认 YAML 位于包内 `resources/analysis.yaml`, 主 Agent 编排 skill 位于 `resources/skills/native-orchestration/SKILL.md`, WebGL2 脚本位于 `rendering/webgl2.js`. 修改资源位置时同步维护 `pyproject.toml` 的 package-data, 并从构建出的 wheel 检查资源与当前公开入口. 仅在可编辑安装中运行成功不足以证明安装包完整.

上面的 wheel 文件名以实际构建输出为准. `check_distribution.py` 只用于本仓库构建的包, 在临时解包目录执行导入及 CLI 帮助检查, 使用当前环境已有依赖, 不安装包或调用真实模型.

禁止读取真实 `.env` 来运行普通测试. 真实模型评估与图片视觉验收需要单独的明确范围, 不由代码结构重构自动触发.
