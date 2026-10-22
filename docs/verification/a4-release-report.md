# Capability Studio A4 / 0.14.3 阶段报告

> HISTORICAL. A4 / 0.14.3 阶段记录；当前发布以机器可读 Release pointer 为准。

## 完成内容

A4 在 A3 `0.14.2` 基线上完成受信算子接入材料闭环，发行版本为 `0.14.3`，Platform schema 为 `0022_operator_proposals`。

- 新增严格的算子、实现和来源材料契约。协议、资源档、运行时和绑定类型采用固定白名单；语义和实现摘要必须与规范内容一致。
- 新增不可变 Proposal Revision、追加式 Review Event 和 Deployment Receipt。`DRAFT/SUBMITTED/CHANGES_REQUESTED/APPROVED/REJECTED` 从事件投影，不覆盖历史。
- 新增四条项目公开路由：创建/列表、精确读取、追加修订和提交评审。VIEWER 只读，EDITOR/OWNER 可提交；公开 API 没有审核或部署路由。
- 新增 `platform-admin operator-proposal-review` 和 `operator-proposal-deployment-import`。CLI 核对固定 Operator Catalog 文件和 Release Lock，部署身份由当前实际目录读取，调用者不能覆盖。
- 实时 `deployment_match` 精确比较 revision、材料、release、catalog、operator、implementation、image、source 和 tests 摘要；`APPROVED` 不等于可执行。
- `ACTIVE/DISABLED/REVOKED` 从当前目录投影；后两者不可执行。现有算子版本的语义覆盖在提交时拒绝。
- Capability Studio 增加“提交接入材料”“评审记录与部署匹配”面板。页面不含安装、审核、部署或目录变更入口，令牌仍只保存在内存。
- 现有 11 个算子、Operator/Checker/Tool Catalog、Execution 和 Agent 路径保持不变。本轮没有新增算法、镜像角色或数据库服务迁移。

详细契约见 [A4 算子接入材料契约](../architecture/a4-operator-registration-contract.md)，升级和维护命令见 [A4 运维说明](../a4-operations.md)。

## 测试结果

### A4 定向及 Authoring 门禁

```bash
uv run --locked pytest -q tests/authoring/a4
# 17 passed

bash scripts/check_authoring.sh --phase operator-registration
# Studio typecheck passed
# Studio 4 tests passed
# Studio production build passed
# 69 Python tests passed
# ruff check passed
# 671 files formatted
```

门禁覆盖空库与 `0021→0022` 升级、重复迁移、材料边界、摘要伪造、幂等与跨项目隔离、历史修订、审核修订错配、同版本语义覆盖、OWNER 无审核路由、维护者 CLI、Release Lock/Catalog 伪造、ACTIVE/DISABLED/REVOKED 投影以及真实浏览器刷新和内存令牌行为。

### 目录与规则回归

```bash
bash scripts/check_p7.sh --phase catalog
# 24 passed
# 13 P1 selection/locator rules passed
# ruff check and format passed
```

Operator Catalog 仍为 11 个计算算子；没有将 Proposal 当作已部署算子，也没有把 Checker 或 SERVICE_TOOL 混入公开计算目录。

### 完整兼容回归

```bash
bash scripts/check_p7.sh --phase compatibility
# 919 passed in 2407.54s
```

P0–P7 的 PostgreSQL、HTTP、Docker、Toil、QGIS、远程恢复、Knowledge、Acceptance、Agent 和独立算子链路全部通过，无失败和必测跳过。按计划未重跑未修改的 P4 长节点性能矩阵。

### 独立发行安装

```bash
uv run --locked python scripts/verify_distribution.py
```

结果通过：11 个 wheel/sdist、core 纯标准库边界、锁定依赖仓库外安装、Platform/Knowledge 迁移、真实 HTTP/OpenAPI、P3 容器闭环、冻结 CWL/Toil、GeoPackage/QGIS/Native、静态 PNG 制图以及 `0001` 历史账本升级。

Release Lock schema v2 固定：

- 11 个 `0.14.3` wheel；
- Operator、Checker、Tool 三个 Catalog；
- 7 个镜像摘要；
- 3 个 Studio 前端资源；
- 384 个源码身份文件。

最终 release digest：`sha256:ad584cb967e4d303073a4f79d3ac32621f277fc548d6a9477d7213206040512d`。

## 问题处理

1. **旧浏览器标签冲突。** A4 初版“接入材料稳定键”被 Playwright 的包含匹配同时识别为 A1“稳定键”。改为“接入材料标识”，A1 与 A4 浏览器用例均通过。
2. **OpenAPI 精确集合未登记 A4 路由。** 完整兼容首轮发现 4 条新公开路由未加入精确断言；更新为仅包含创建/读取/修订/提交，继续明确禁止 web 审核和部署路由。
3. **CLI 依赖边界。** 初版 infrastructure CLI 直接导入 contracts 的 Release Lock 工具，违反既有分层。新增只依赖标准库的 core 部署证据核验用例，infrastructure 只依赖 core；A4、架构及 API 共 61 项定向回归通过。
4. **完整兼容首轮结果。** 首轮为 917 通过、2 失败；上述两项修复后完整重跑为 919 通过、0 失败。

没有通过吞错、跳过测试、模拟部署匹配或扩大项目权限处理问题。

## 范围说明

A4 支持管理接入申请和精确证明“某次受控发行是否与某个已审核修订一致”。它不支持用户代码、自动获取来源、自动构建、项目自审批、网页部署、动态插件加载、算子市场或远程绑定创建。新增算法仍需代码开发、受信测试、镜像构建和目录发行。

A5 的条件、scatter、子工作流、JSON/YAML 自包含导入和源码/画布高级往返没有路由、表或占位成功响应。

## 阶段结论

A4 通过。接入材料、评审历史和实际运行能力能够精确对应；项目用户不能借 Studio 绕过目录、Release Lock、维护者权限或既有执行准入。完整回归和独立安装无失败或必测跳过。本轮结束，不自动进入 A5。
