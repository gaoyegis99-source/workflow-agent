# Capability Studio A7 / 0.14.6 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

A7 在 A6 `0.14.5` 的不可变工作流 Release 上增加生命周期治理和确定性的撤销竞态仲裁，发行版本为 `0.14.6`。Platform schema 升至 `0023_workflow_release_lifecycle`；Data、Execution、Knowledge schema 不变。

- Release 生命周期固定为 `ACTIVE`、`DISABLED`、`REVOKED`。只有 OWNER 可以通过带预期修订的幂等命令追加生命周期事件；`REVOKED` 为终态。
- 升级前 Release 回填为 `ACTIVE / revision 1 / INITIAL_RELEASE`，source、Package、Trial、Action 和成果历史均不改写。
- `DISABLED` 阻止新的正式 Release 执行，但已接受 Action 可以按原身份恢复、取得节点许可并发布。
- `REVOKED` 阻止新执行、尚未取得许可的新节点和首次发布许可；已运行计算仍按真实外部事实查询或取消。
- 节点许可和发布许可都使用持久事实。许可先提交时，响应丢失后的同一节点或 Action 可恢复原决定；撤销先提交时，后续许可稳定拒绝。
- 正式 Release Action 冻结准入时的 Release 身份、生命周期和修订。最终准入事务重新锁定 Release；准备期间发生状态变化时不留下 Action、预算或 outbox 半状态。
- 原幂等键的合法重放先返回既有 Command，Release 后续停用或撤销不会制造第二个 Action。
- Studio 增加生命周期状态、历史和 OWNER 控件；停用版本不可从版本库新执行，刷新后完全由 API 重建状态，令牌仍只存在内存。

完整规则见 [A7 生命周期契约](../architecture/a7-workflow-release-lifecycle.md)，升级和回退见 [A7 运维说明](../a7-operations.md)。A7 没有增加 marketplace、跨项目共享、Agent 工作流编写或科学验收捷径。

## 测试结果

### A7 与 Authoring 门禁

```bash
uv run --locked pytest -q tests/authoring/a7/test_release_lifecycle.py
# 12 passed

uv run --locked pytest -q tests/authoring/a7/test_browser.py
# 1 passed

bash scripts/check_authoring.sh --phase release-lifecycle
# Studio 2 files / 7 tests passed
# Studio production build passed
# A1–A7 Python: 117 passed in 181.39s
# ruff check passed
# 693 files formatted
```

定向测试覆盖状态转换、并发修订、OWNER 权限、迁移回填、HTTP/OpenAPI、停用后的幂等恢复、停用后既有执行延续、撤销前后节点许可、撤销前后发布许可，以及真实浏览器的停用、恢复和历史展示。

### 目录、架构与完整兼容

```bash
bash scripts/check_p7.sh --phase catalog
# 24 passed；13 项 P1 选择/定位规则通过

uv run --locked pytest -q tests/architecture/test_boundaries.py tests/integration/test_api.py
# 44 passed

bash scripts/check_p7.sh --phase compatibility
# 919 passed in 2411.90s (0:40:11)

uv run --locked ruff check .
uv run --locked ruff format --check .
# PASS；693 files already formatted
```

完整兼容覆盖 P0–P7、A1–A6 的 PostgreSQL、HTTP、Docker、Toil、QGIS、远程执行、Knowledge、Acceptance、Agent、审核算子和历史 Authoring 行为，无失败或必测跳过。按既定规则未重跑未修改的 P4 长节点性能矩阵。

### 独立发行安装

```bash
uv run --locked python scripts/verify_distribution.py
```

独立构建与仓库外安装通过：11 个 wheel/sdist、core 标准库边界、锁定依赖、API/OpenAPI、四套数据库迁移和 schema 检查、管理 CLI、控制与 Authoring Worker，以及 P3、冻结 CWL/Toil、GeoPackage/QGIS/Native 和静态制图真实容器闭环。

Release Lock schema v2 固定 11 个 `0.14.6` wheel、三个 Catalog、七个不可变镜像摘要和三个 Studio 资源。最终 release digest 为 `sha256:45bcfd0492fec961ccee4c1803c841498b8ede7afaeb3e24e3bd5a2101f4dedb`。

## 问题处理

1. **迁移 SQL 的冒号被解释为绑定参数。** 初始 `0023` 回填原因使用带冒号的字符串，Alembic/SQLAlchemy 将其识别为 bind。改为无歧义的固定初始原因后，空库、`0022→0023` 和重复迁移通过。
2. **旧算子发布拒绝响应发生兼容变化。** A7 首版为所有发布拒绝增加了 `reason`，使旧协议的精确响应测试失败。将 A7 诊断字段限定为正式 Release 上下文，普通算子继续返回原 `{"decision":"DENY"}`。
3. **节点许可响应丢失后的撤销竞态。** 初始实现会在节点已取得计算槽后因 Release 撤销而拒绝同一节点重放。计算槽记录本身即为已提交许可事实；现在原节点恢复原许可，新节点仍被撤销阻止。
4. **浏览器生命周期历史可见性。** Studio 的历史详情默认折叠导致自动化无法稳定核验。页面保留语义化详情并由测试显式展开，确认状态、修订和原因来自 API，且没有浏览器持久令牌。

## 范围与结论

A7 支持项目内不可变工作流版本的临时停用、恢复和永久撤销，并用数据库行锁及持久许可消除撤销与节点启动、成果发布之间的歧义。生命周期不删除 Release、计算记录或已发布资产，也不把 `SAMPLE_VERIFIED` 提升为科学 `VERIFIED`。

本轮所有必测项均通过，结论为 **A7 / 0.14.6 通过**。本轮在此结束，不自动进入 A8、marketplace、跨项目共享、Agent authoring 或 P8。
