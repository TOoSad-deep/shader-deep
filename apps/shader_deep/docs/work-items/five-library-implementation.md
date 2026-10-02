# 五库分析实施记录

日期: 2026-09-30.

状态: 首版代码与用户输入修复已完成并通过本地验收; Git 交付见下文. 本记录只描述实施证据, 数据字段和角色契约分别由[五库设计](../analysis-five-libraries.md)与[架构设计](../analysis-architecture-design.md)维护.

## 范围与职责

- 五库领域: `domain/five_libraries/`, 已确认对象字段、来源 ID 规范化、显式文本引用、受限合并和冲突校验.
- 任务运行: `runtime/task_store/`, 唯一原子提交记录、逻辑任务/attempt、恢复计数、幂等、终态和代次恢复.
- 角色与调度: `agents/five_analysis/`、`workflows/five_analysis.py`, 单目标、默认三视角、一批独立探索、异步 F/R/M 整合及 V0/V1.
- 公开交付: `workflows/analysis.py`、CLI、`infrastructure/storage/report_package.py`, 完整文件包、短入口和按草图视图.

旧四库入口移至 `workflows/legacy_analysis.py`; 历史回放和回归显式使用旧链路. 公开函数参数签名保持不变, `AnalysisOutcome` 增加默认为空的 `report_dir`; 新输出协议为 `five_libraries_v1`, 公开结果只索引文件包而不复制完整正文. 默认 `max_tasks` 从 6 改为 3, 新入口拒绝超过三个视角.

草图最低数量仍待讨论. 当前不要求每个 worker 产出至少一套, 已有草图漏掉自身来源目标则需保留明确缺口, 不把三份草图合成一份. 未加入新的业务对象、数据库、跨图检索或分析到生成自动交接. 主 agent + skill 的具体加载仍未确定, 当前独立角色实现不冒充 skill 系统接入.

## 验证

2026-09-30 首版实施时的未提交工作区验收, 代码状态绑定下面的验证证据; 后续修复另行记录:

| 检查 | 结果及范围 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check` | 通过: 0 个文档/依赖问题, 460 项单元测试, Ruff、格式和 ty |
| 缓存的 setuptools PEP 517 backend `build_sdist` / `build_wheel` | 通过; 使用既有 setuptools 83.0.0 与 wheel 0.48.0, 未安装或改动依赖 |
| `scripts/check_distribution.py` | 通过: 资源、导入隔离、全部模块与 6 个 CLI 帮助入口 |
| `git diff --check` | 通过; 未暂存或提交 |
| 独立复核 | 领域/角色交接、任务恢复和根交付边界已复核, 发现的问题已修复 |

完整命令输出、失败历史、制品及代码输入指纹见[验证证据](evidence/five-library-implementation-2026-09-30.json). 最初隔离构建因 DNS 无法下载构建依赖, 当前环境也未安装 backend; 改用本地已有缓存 backend 完成标准 sdist/wheel 构建, 未修改配置或锁文件. 过渡期缺导出和测试模型分支问题的完整输出保留, 不把中间失败作为当前状态.

单元模型经真实角色/工具/运行存储链路执行, 仍不证明真实服务、视觉合并语义或上下文收益. 本轮未修改渲染器, 未重跑浏览器或真实外网模型测试. 初稿共同观察与草图语义合并未进入新链路.

## 复核发现与修复

- 首次领域测试发现 `Relation` 漏导出, 已补齐.
- 只读复核发现调用计数在协调器重启后会重置, 已将调用计数纳入持久任务记录, 主调用在共享锁内汇总并扣费.
- 只读复核发现整合中断可能跳过 V0 交付, 已补中断/异常回退及封存路径.
- 只读复核发现已有草图可能遗漏自身来源目标而未记缺口, 已按已确认字段契约修复, 不增加草图数量门槛.
- 整合完成前要求实际读取 F/R/M 正文, 空合并建议不能绕过必要语义检查.
- 已加入同一提交记录中的最终交付投影. 部分包准备完成但封存失败后, 恢复不重算缺口、不修改八个包文件、不新增模型请求.
- 主线程中断先取消全部在途任务及恢复决策, 再等待清理. 恢复登记与取消交错时, 父任务状态守卫在请求计数/新提交的同一锁内复核, 迟到子任务也不能写入.
- 初次及自动重派仍失败后, 主 agent 在受限上下文诊断并决定最后重派或结束; 恢复自身失败不递归, 共享主额度跨并发/重启不归零.
- 已提交结果及版本读取核对内容指纹, 合法 JSON 被篡改也不会冒充原版本.
- 必填文本拒绝全空白, 保留合法正文原样. 运行记录保存来源本地 ID 到 V0/V1 对象的映射与原始工具/模型用量审计.

## 分支审查后的用户输入修复

2026-09-30 只读分支审查发现 `run_analysis_task` 只传递 `TaskRecord.objective`, 丢失绑定目标的 `request`、`constraints` 和 `protected_features`. 已恢复原用户要求、显式约束、保护特征的完整装配, 不同的任务 objective 作为补充; 相同提示词不重复拼接. 装配使用 `task.target_version` 对应目标, 不改变已登记黑板记录.

新增回归测试经真实无网络角色链核对规划及三个探索请求, 覆盖不同目标的排除、完整用户要求传递、固定输入保存与简单 PNG 入口的去重. 修复前该回归明确失败, 修复后 `make check` 通过: 462 项单元测试、0 个文档/导入问题、Ruff、格式和 ty 全部通过, `git diff --check` 通过. 完整失败输出及新代码输入指纹见[修复验证证据](evidence/five-library-user-input-fix-2026-09-30.json). 本次未重跑真实模型、浏览器或 wheel 构建, 首版构建证据继续对应其原始代码状态. 验证证据记录修复当时的未提交工作区, 后续 Git 交付另记.

## Git 交付

2026-09-30 用户授权 commit and push 后, 五库实现、设计图表、回归测试及用户输入修复提交为 [16a2a862](https://github.com/TOoSad-deep/shader-deep/commit/16a2a862c8a379bc5c44cd99af41e30b1491557e), 消息为 `feat(repo): add single-element five-library analysis`. 已推送到 `origin/TOoSad-deep/repo/structure-refactor`; `git ls-remote --heads origin TOoSad-deep/repo/structure-refactor` 返回的完整 SHA 与实现提交一致. 本节记录已核实的实现提交, 不将随后的文档记录提交混为实现提交.

仅暂存本轮明确的 50 个实现、测试、设计及验证文件. 单独生成的 `docs/discussions/` 讨论归档保留在工作区, 未纳入此次提交.

2026-09-30 用户授权合并到个人仓库 main 后, 核实 `origin` 仓库的实际默认主分支为 `TOoSad-deep/repo/shader-deep-main`. 将该分支从 `8535e429adaf5c75c2372e391eed439a7a8fee8a` 快进到包含结构重构、五库实现与用户输入修复的 `f692ec2623e089d5e97d438db2310dc37b347c0d`, 并推送成功. `git ls-remote --heads origin TOoSad-deep/repo/shader-deep-main` 返回同一 SHA. 快进后的代码树与已验证工作分支相同, 没有重新组合代码或创建冲突修复; 后续文档提交仅补记这一已确认的主分支交付事实.

## 后续边界

完成本地验收后仍需真实模型参考图验证, 核对视觉分析独立性、合并语义和实际上下文用量. 草图最低产出与 skill 封装继续按原讨论进程确定. 这些待办不改变已经确认的五库字段与职责边界.

## 2026-10-02 交付读取与入口修复

用户在交付审查后授权修复两项问题. 基于 `cb28bc14` 的工作区修改, 未提交或推送:

- `read_sketch` 显式选择局部备选时, 将其 `reason` 与 `conditions` 中的对象引用加入取材范围, 复用既有正文及关系参与项的递归加载. 未选备选继续按需读取, 不改写已发布文件包或有效机制选择.
- 短入口从同一五库快照派生草图默认机制的 ID 与名称. 按首次出现去重, 最多展示三个机制, 长名称压为单行并截断; 超量内容提示回读完整库. 不新增模型调用、业务字段或方案排名.
- 新增三项回归测试, 覆盖理由/条件两种引用入口、递归依赖、未选备选隔离、文件包不变、同名方案区分及有界导航. 同步应用 README; 公开函数签名保持不变.

验证命令均在应用目录执行: `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache uv run --no-sync python -W error -m unittest tests.unit_tests.infrastructure.test_report_package -q` 通过 9 项; `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check` 通过 514 项 unittest、repository_check (0 findings)、Ruff、格式和 ty. 完整检查日志保存在 `/private/tmp/shader-deep-report-fix-20261002-check.log`; `git diff --check` 通过.

本次未调用真实模型或浏览器, 未增加分析到生成的自动交接, 未重新生成历史交付包. 真实可用五库样本及 shader 生成接入仍需另行验证.
