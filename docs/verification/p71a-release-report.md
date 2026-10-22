# P7.1a / 0.11.0 独立表格算子试点报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增独立发行包 `groundwater-tabular-operator-pack`，只依赖标准库与 PyArrow，不导入平台算子、数据库或服务内部模块。
- Operator Catalog 增加 `table-profile@1.0.0`、`table-validate@1.0.0`、`TABULAR_V1` 和 `table-small-v1`，两者标记为 `CURATED_PACKAGE / VERIFIED / ACTIVE`。
- 新增严格 TableSelection、Profile 与六类 Validate 契约；Data 精确解析受信 Parquet 表示，Execution 通过适配端口核验 Schema 和固定输出集合。
- 验证失败固定映射为 `FAILED / VALIDATION_FAILED`，消耗已开始的一次额度，不发布成果，只保存受限汇总。
- 单算子、并行独立算子、验证失败原子中止，以及 P3 内置算子到独立 Profile 的真实 Toil 工作流均已跑通。
- Agent v2 只执行 TaskSpec 冻结的表格意图；资产、字段、规则、版本、实现和资源均不能由模型改写。
- 11 个工作区发行包统一为 `0.11.0`；数据库 schema 不变。

## 测试结果

- `tests/p71a`：32 项通过，使用真实 PostgreSQL、HTTP、Docker、独立镜像和真实 Toil。
- `tests/p70`：24 项通过；P1 选型规则 13 项通过。
- Architecture、P3 Execution 和新增输出防篡改定向组：72 项通过。
- P6.3–P6.10 受影响 Agent 与真实科学闭环：87 项通过。
- P0–P6/P7.0 完整兼容入口：796 项通过。
- Ruff 检查与格式检查通过。
- 11 个 wheel 构建成功；在仓库外 Python 3.12.8 环境安装后，11 个发行版本和核心导入均通过。
- 固定 tabular 镜像：`sha256:f3da161a39f5a314e5e1691ac38841a28c4adff4d356b2a6fd08bcfd963eb3c2`。

非重复门禁合计 865 项，无失败、错误或跳过。本轮没有性能门槛，也没有重跑 P4 长节点性能矩阵。

## 问题处理

- 强化输出 Schema 后，首次混合工作流把根输入的 `binding.table` 与派生输入的 `binding.manifest` 当成不同契约，合法的内置成果被拒绝。现已统一解析受信表格描述，混合链路重跑通过。
- 初版 Execution 应用层直接导入 PyArrow，违反服务分层。Parquet 读取现已移动到适配层并通过端口注入，架构边界与执行回归均通过。
- 全量回归后段遇到 Docker 预定义网段耗尽。测试 PostgreSQL 仍使用唯一容器、卷和 localhost 端口，仅在无法创建临时网段时回退共享 bridge；此前环境错误组重跑全部通过。
- 首次仓库外安装误用了系统 Python 3.13，正确拒绝项目的 `<3.13` 约束；改用锁定支持的 Python 3.12.8 后通过。

## 范围和限制

本轮只提供两个审核表格算子。没有页面、用户镜像、用户代码、动态包安装、新 checker 或数据库迁移。Profile 只提供受限描述统计；Validate 是确定性规则检查，不证明现实数据或领域语义正确。

## 发行身份

- Operator Catalog：`sha256:f2e75b679e609ba8b6aa9433286d5e2406e4024e698d271ae68c2fcec6867488`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- Release：`sha256:fe5f78131f93a803f5ed3efcc20c026848fb35bc34ef28ce4497b787d9a91e0d`

## 阶段结论

P7.1a 在全部门禁通过后结束，不自动迁移其他表格算子。
