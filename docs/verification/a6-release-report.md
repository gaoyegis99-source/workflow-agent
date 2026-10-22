# Capability Studio A6 / 0.14.5 阶段报告

> HISTORICAL. A6 / 0.14.5 阶段记录；当前发布以机器可读 Release pointer 为准。

## 完成内容

A6 在 A5 `0.14.4` 基线上补齐项目工作流版本库和不可变 Release 正式执行，发行版本为 `0.14.5`。Platform schema 仍为 `0022_operator_proposals`，Data、Execution、Knowledge 均无迁移。

- 新增项目级 Release 库，跨 Definition 分页返回稳定键、历史 metadata、规范 SemVer、精确 PackageRef、source/context 身份、样例资格和当前可执行原因。
- 新增按精确 Definition 与 SemVer 的正式执行入口。客户端只提交 Task、输入和节点额度，不能提交或覆盖 PackageRef、算子实现、镜像或命令。
- 服务端在首次准入时解析 Release，并在最终事务重新核对不可变身份。Action 冻结 Release、Definition、稳定键、版本、Candidate、Revision、source、Package、context 和 `SAMPLE_VERIFIED` 资格。
- 正式执行复用现有 Task、grant、预算、Action、ExecutionPlan、Toil、节点许可、恢复、取消和 Data 原子发布，没有增加第二套调度状态机。
- 幂等重放在 Data 解析和图展开前返回原 Command；同键异内容冲突。首次新执行仍检查当前权限、Release 可执行性、精确 grant、预算和 Task 状态。
- Studio 新增项目版本库和“从版本库运行”面板，明确说明 `SAMPLE_VERIFIED` 不等于科学 `VERIFIED`。令牌继续只放内存。

完整边界见 [A6 契约](../architecture/a6-workflow-release-library.md)，部署与回退见 [A6 运维说明](../a6-operations.md)。A6 没有开放 public marketplace、跨项目共享、latest、Agent 工作流编写或任意代码。

## 测试结果

### A6 和 Authoring 门禁

```bash
uv run --locked pytest -q tests/authoring/a6
# 6 passed

bash scripts/check_authoring.sh --phase release-library
# Studio 2 files / 7 tests passed
# Studio production build passed
# A1–A6 Python: 104 passed in 168.38s
# ruff check passed
# 686 files formatted

uv run --locked pytest -q tests/authoring/a6 tests/integration/test_api.py
# 17 passed
```

定向测试覆盖 DTO 注入拒绝、项目隔离和分页、历史 metadata、精确 Release 身份冻结、授权失败全回滚、预算预留、幂等重放先于外部依赖、同键异内容冲突、HTTP/OpenAPI 和真实浏览器版本库重建。

### 目录、架构与完整兼容

```bash
bash scripts/check_p7.sh --phase catalog
# 24 passed；13 项 P1 选择/定位规则通过

uv run --locked pytest -q tests/architecture/test_boundaries.py tests/integration/test_api.py
# 44 passed

bash scripts/check_p7.sh --phase compatibility
# 919 passed in 2425.82s (0:40:25)
```

完整兼容覆盖 P0–P7 的 PostgreSQL、HTTP、Docker、Toil、QGIS、远程执行、Knowledge、Acceptance、Agent、审核算子及 A1–A5 行为，无失败或必测跳过。按既定规则未重跑未修改的 P4 长节点性能矩阵。

### 独立发行安装

```bash
uv run --locked python scripts/verify_distribution.py
```

独立构建与仓库外安装通过：11 个 wheel/sdist、core 标准库边界、锁定依赖、API/OpenAPI、四套数据库迁移和 schema 检查、管理 CLI、控制与 Authoring Worker，以及 P3、冻结 CWL/Toil、GeoPackage/QGIS/Native 和静态制图真实容器闭环。

Release Lock schema v2 固定 11 个 `0.14.5` wheel、三个 Catalog、七个不可变镜像摘要和三个 Studio 资源。最终 release digest：`sha256:94aaef055a01d7581fbe6152d22876b50aaee34a7e5cc3ee735791a1ca0ff497`。

## 问题处理

1. **Trial 与 Release 校验块混排。** 初次 A6 定向测试为 2 通过、3 失败。正式 Release 准入错误读取了只在 Trial 分支定义的 Candidate 局部变量。将 Candidate 重核放回 Trial 分支并独立执行 Release 重核后，A6 5 项后端/HTTP测试通过。
2. **Studio 可访问名称冲突。** 首次 A1–A6 合并门禁为 101 通过、3 失败。新增正式执行面板的标题和标签包含旧 A3 控件名称，Playwright 模糊匹配得到两个元素。改成独立的“从版本库运行”“版本节点额度上限”和“版本输入 JSON”后，真实表格、QGIS/制图和浏览器回归 3/3 通过；完整 Authoring 门禁最终 104 项通过。
3. **格式门禁。** 首轮功能测试已全部通过，但 Ruff 报告两个 A6 测试文件格式不一致。格式化后全库 Ruff check 与 686 文件 format check 通过，业务代码未改变。

## 范围与结论

A6 支持项目内发现和执行不可变 Release，并把用户选择的版本身份带入权威 Action。Release 当前不可执行会阻止首次新提交，但不会改写历史；已接受 Action 的恢复仍使用原 Package、节点和预算身份。

A6 不提供科学自动完成。需要科学结论的成果仍要登记精确 Deliverable，并由既有 CoveragePlan 与独立 Checker 形成 `VERIFIED` Completion。

本轮所有必测项均通过，结论为 **A6 / 0.14.5 通过**。本轮在此结束，不自动进入 marketplace、跨项目共享、Agent authoring 或 P8。
