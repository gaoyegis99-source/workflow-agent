# P7.0 收口 / 0.10.1 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 生产模式强制 Operator、Checker、P7 v2 Tool Catalog 和 release lock，不再使用源码占位目录或占位镜像。
- Platform、Execution 与 Agent 校验同一发行和目录摘要；Execution 同时校验实际容器镜像。
- 新 AgentRun 冻结 Tool Catalog 与 Operator Catalog，新运行使用 `execution.submit_operator@2.0.0`；Worker 拒绝目录错配。
- Operator Catalog 增加文件大小、目录数量、实现数量、端口、递归深度和结构总量上限。
- 本地与远程实现改为严格判别联合，远程绑定不再伪造本地镜像字段。
- 生命周期变更采用停机、排空、重新生成 release lock 和整体启动，不引入在线撤销仲裁或数据库迁移。

## 测试结果

- `tests/p70`：24 项通过。
- P1 选型规则：13 项通过。
- Contracts、Integration、Architecture、P2、P3：322 项通过。
- P4 全阶段功能与故障回归：228 项通过。
- P5 全阶段：86 项通过。
- P6 全阶段：159 项通过。
- 合计 832 项，无失败、错误或跳过。
- Ruff 检查和格式检查通过。
- 10 个 `0.10.1` wheel 构建成功；在仓库外 Python 3.12.8 环境安装后，版本、导入、目录双实现校验和 `uv pip check` 通过。

本轮没有重跑 P4 的长节点性能矩阵。P4 功能、容器、故障恢复和远程组合测试已完整执行；本轮目标是逻辑与发行边界，不设性能门槛。

## 问题处理

- 首次兼容回归发现 Core 反向依赖 Contracts；已改为 Core 仅通过标准库读取部署模式，恢复分层边界。
- 严格镜像核验最初阻断 P3 的开发故障镜像；已限定为生产模式或显式目录部署强制，生产约束不降级。
- Agent 目录注入最初把无 v2 Tool Catalog 的历史 P6 测试视为半配置；已让生产强制成对注入，开发历史路径保持兼容。
- 首次仓库外验证误选 Python 3.13，依赖检查正确报告项目要求不兼容；改用受支持的 Python 3.12.8 后完整通过。

## 版本与范围

版本统一为 `0.10.1`。Platform、Data、Execution、Knowledge schema 均保持原版本，没有迁移。本轮没有新增算子、在线热更新、项目自定义镜像或用户代码执行。

发行锁为 `p701-release-lock.json`，release digest：

```text
sha256:e3a1277122922d6b462cc4efd836dc00039fbcdcc4544951d9b28c5769afde0e
```

## 阶段结论

P7.0 收口通过。下一轮可以进入 P7.1a 的 `table-profile`、`table-validate` 独立包试点；本轮不自动实施。
