# 历史执行协议与兼容边界

这里保留仍需支持的旧执行流程. 当前入口和角色地图见[应用架构](../../../docs/architecture.md); 默认工作流不从本目录导入实现, 新 Agent 不以旧会话为基类.

## 先区分三类兼容

| 类型 | 位置与用途 |
| --- | --- |
| 旧导入路径的薄转发 | `shader_deep.analysis`、`context`、`tools` 及旧单文件模块; 为调用者保留名称, 具体去向需看实际 import |
| 历史执行流程 | 本目录的 `analysis/`、`ondemand/` 与 `controlled.py`; 只有显式调用或兼容测试才运行 |
| 历史数据契约 | [domain/legacy.py](../domain/legacy.py) 与[业务验证](../domain/validation.py); 支持记录读取和引用校验, 不代表运行旧 Agent |

旧导入名不一定意味着旧行为. 例如[旧分析入口](../agents/analysis.py)转发到当前工作流; [analysis/controlled.py](../analysis/controlled.py)则保留历史受控会话. 判断时看实际绑定, 不凭文件名猜测.

## 历史流程地图

| 入口 | 保留的能力 | 当前用途 |
| --- | --- | --- |
| [analysis/session.py](analysis/session.py)、[analysis/worker.py](analysis/worker.py) | 视角报告、测量、引用读取和旧综合协议 | 显式旧调用及其行为回归 |
| [ondemand/session.py](ondemand/session.py) | 可能性库的按需目录读取、测量、比较与显式结束 | 保留宽工具协议及恢复/材料准入用例 |
| [controlled.py](controlled.py) | 程序固定清单的受控比较 | [固定样本实验](../experiments/controlled_replay.py), 不接管默认整合 |

当前标准[冻结回放](../workflows/replay.py)调用默认整合角色. 它恢复经过验证的初始探索输入, 不恢复上次整合决定、旧模型历史或完整运行会话.

## 修改与保留规则

1. 公共入口和历史数据支持是不同契约, 不能因为当前流程不调用某个类就删除它.
2. 兼容实现可以使用当前业务和执行组件; 当前角色、工作流及公共执行组件不能反向依赖旧会话.
3. 修复公共预算、重试、提交或存储逻辑时, 同时检查受影响的兼容用例. 兼容测试通过只证明该协议, 不代表当前默认流程通过.
4. 工具权限、来源、实际呈现和版本校验仍生效. 不为兼容调用增加更宽的隐式写入路径.
5. 不把旧报告自动解释为当前四库. 当前入口拒绝未经显式转换的历史输入, 已支持的数据读取行为继续保留.

删除兼容层前, 单独记录真实调用方、保留的数据格式、替代入口、迁移方式和用户确认的删除范围. 没有调用证据不等于没有调用者. 此次重构没有做删除授权或迁移承诺.

## 修改后如何验证

在 `apps/shader_deep/` 执行:

```sh
uv run --no-sync python -W error -m unittest discover -s tests/unit_tests/compatibility -q
make check
```

[旧公开入口测试](../../../tests/unit_tests/compatibility/test_public_api.py)检查真实导入与导出身份. 修改转发或打包时, 按[开发指南](../../../docs/development.md)重新检查构建出的 wheel, 不只检查源码工作区能否导入.
