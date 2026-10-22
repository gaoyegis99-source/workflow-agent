# P7.1b / 0.11.1 表格算子可信验收闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- Checker Catalog 新增 `tabular-profile-reproduction-v1` 与 `tabular-validation-reproduction-v1`。两项能力精确匹配算子、版本、`report` 端口和结果 Schema，同时覆盖 `DATA_VALIDITY` 与 `RESULT_REPRODUCTION`。
- `groundwater-checker-pack` 增加独立表格复算。实现不导入、也不安装 `platform_tabular_operators`；PyArrow 只负责读取 Parquet，统计和六类规则均由 checker 独立实现。
- Profile checker 独立核对字段顺序、物理类型、行数、空值、不同值、NaN/Inf、数值范围、Unicode 长度和布尔计数，并交叉验证 report、table、method、manifest 和 provenance。
- Validation checker 独立重跑 `ROW_COUNT`、`TYPE`、`NOT_NULL`、`UNIQUE`、`RANGE` 和 `ALLOWED_VALUES`。伪造的成功报告不能通过；`FAILED / VALIDATION_FAILED` 仍无成果、Deliverable 或 Completion。
- Execution 能从直接成果和工作流成果的最终 publication manifest 暂存精确源 Parquet 与三种成果表示。所有项目、资产、表示、大小和 SHA-256 在进入 checker 只读目录前重新核对。
- CoveragePlan 保持运行完整性前置；两个领域检查项合并为一次领域 checker run。未知算子、错误版本、端口或 Schema 保持 `UNCOVERED / INDETERMINATE`，没有通用 PASS 兜底。
- Agent v2 已跑通“登记分析 → 提交冻结算子意图 → 精确 Deliverable → 开放验收 → VERIFIED Completion”。单算子使用 3 次额度；同时验收 Profile 与 Validation 使用 5 次额度和 3 个 checker run。
- 工作区、11 个 wheel、普通/GDAL checker、tabular 镜像和 CWL runtime 统一到 `0.11.1`。四个数据库 schema 保持不变。

## 测试结果

- `bash scripts/check_p7.sh --phase tabular-acceptance`：11 项通过，0 失败、0 错误、0 跳过；使用真实 PostgreSQL、HTTP、Docker、固定 checker/tabular 镜像和 Agent Worker。
- `bash scripts/check_p5.sh --phase all`：P5 定向 86 项通过；完整 pytest 880 项通过；响应丢失 1 项和 P1 规则 13 项通过。统一入口最终以 0 退出。
- `bash scripts/check_p7.sh --phase compatibility`：863 项通过，0 失败、0 错误、0 跳过。
- Ruff 检查和格式检查通过。
- 11 个 `0.11.1` wheel 与源码包构建成功；仓库外 Python 3.12 环境安装、API/Knowledge/Data/Execution 迁移、真实 HTTP、P3 容器、CWL、QGIS、Native GDAL 和制图闭环全部通过。
- 固定普通 checker 与 GDAL checker 容器探针通过；镜像、Catalog、wheel 和源码摘要已写入 release lock。

上述测试存在覆盖重叠，不把同一用例重复累计。按完整 pytest 880 项加独立 P1 规则 13 项计算，非重复门禁至少 893 项，均无失败、错误或跳过。

## 问题处理

- 空间镜像构建仍硬编码旧 `0.7.2` wheel，导致统一门禁无法准备 QGIS/Native 镜像。构建脚本现从根发行版本取值，并使用当前 wheel；真实 QGIS 3.44.15 与 GDAL 3.8.4 镜像重建和闭环通过。
- CWL 子工程版本已升到 `0.11.1`，独立 `uv.lock` 尚未同步。锁文件更新后，`uv sync --locked` 和 CWL 镜像构建通过。
- 仓库外发行探针仍断言健康阶段为 P5.3、且 OpenAPI 不应存在 Agent 路由。探针已按当前 P6.2 公开契约更新，随后 11-wheel 外部安装全路径通过。
- 表格能力在模块顶层导入 PyArrow，使独立 GDAL checker 也要求表格运行时。导入现只在两个表格 capability 被调用时发生，GDAL 镜像无需携带 PyArrow，两个固定镜像探针均通过。
- Data 与 Knowledge 的 OpenAPI 版本仍固定为 `0.7.2`。两者现在从已安装发行元数据读取版本，后续升级不会再次漂移。
- checker 构建和探针中的历史版本文本已改为当前工作区版本；每次最终源码变化后均重新构建镜像并核对 `org.opencontainers.image.revision`。

## 范围与限制

本轮没有新增计算算子、公开路由、页面功能或数据库迁移。没有新增 `CLAIM_SUPPORT`；要求该检查类型的表格 VERIFIED 验收仍为 `INDETERMINATE`。checker 证明发布身份、结构和固定统计/规则结果可复算，不证明现实数据来源或业务语义真实。

## 发行身份

- Operator Catalog：`sha256:62991a4bd4610aa5fa9dd21258185130432341ed818e03a62c71c9adf1e49ee2`
- Checker Catalog：`sha256:a09ca063eca65355a5a76ed4e2218be1b46fe05eb705d05a96da6d6811d617e7`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- Source tree：`sha256:88f91059d41cca7fca75079be645d73deb8084b6d99f0bcd4b877d7d006d1094`
- Release：`sha256:c453c4d3d6841aa485ec3393f24241e6db409b3c4bf9dc7530b8383ab862d899`

完整结构化证据见 `p71b-release-evidence.json`，目录与发行冻结文件见同目录下的 `p71b-*-catalog.lock.json` 和 `p71b-release-lock.json`。

## 阶段结论

P7.1b 全部门禁通过，发布版本为 `0.11.1`。两个审核表格算子现在能够经独立复算形成 VERIFIED Completion；本轮结束，不迁移其他表格算子。
