# P7.2b / 0.12.1 表格过滤闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增审核算子 `table-filter@1.0.0`。它使用最多 128 个节点、深度最多 8 的拓扑排序谓词树，支持固定比较、集合、空值、NaN、有限值和布尔组合；不接受 SQL、正则表达式、脚本或任意表达式。
- 冻结 `kleene-three-valued-v1`：普通比较遇到 null/NaN 得到 UNKNOWN，NOT 不会把 UNKNOWN 变成 TRUE，只有 TRUE 行进入结果。输出保持字段身份、Schema、物理类型和原行顺序，零行结果合法。
- Platform 在 Action、预算和 outbox 写入前，用纯标准库核对谓词字段与 Data 受信 Schema；Execution 在创建直接或工作流节点容器前再次核对；独立算子还会重新验证完整谓词结构和类型。
- `groundwater-tabular-operator-pack` 增加 `table-filter-native-v1`，固定输出 `report/table/method`。Execution 核对文件集合、Schema、参数、谓词摘要、行数与三值计数守恒后，才允许 Data 原子发布 `OPERATOR_RESULT`。
- 新增 `tabular-filter-reproduction-v1`。checker 不导入生产算子，独立解析谓词、重算每行三值结果、核对保留行及 report/method/manifest/provenance。能力精确匹配 Filter 的 `report` 和 `table-filter-report-v1`。
- Agent v2 可提交 TaskSpec 已冻结的 Filter 意图，经运行完整性和独立复算形成 VERIFIED Completion。直接闭环使用 3 次执行额度。
- 新增真实 `Project → Filter → Profile` 三节点冻结工作流。派生输入使用上游已核验 manifest；三个节点成功后整组原子发布，预算各结算一次。
- 11 个 Python 发行包和 CWL runtime 统一为 `0.12.1`。Operator Catalog 包含 9 个算子，Checker Catalog 包含 9 个能力；数据库 schema 与公开路由均不变。

## 测试结果

- `uv run --locked pytest -q tests/p72b`：15 项通过，0 失败、0 错误、0 跳过，耗时 52.19 秒。包含真实 PostgreSQL、HTTP、Docker、Agent VERIFIED、两个 checker 和三节点 Toil 工作流。
- P7.1a、P7.1b、P7.2a、P7.2b 定向联合回归：68 项通过，0 失败，耗时 271.08 秒。
- `bash scripts/check_p7.sh --phase compatibility`：889 项通过，0 失败、0 错误、0 跳过，耗时 2293.59 秒（38 分 13 秒）。
- `bash scripts/check_p7.sh --phase catalog`：P7 目录 24 项通过；P1 规则 13 项通过；Ruff 和格式检查通过。
- 架构边界与 Filter 定向测试：46 项通过，验证 Core 只依赖标准库。
- `uv run --locked python scripts/verify_distribution.py`：11 个 wheel 和源码包构建成功；核心标准库边界、仓库外锁定依赖安装、API/OpenAPI/停机、四服务迁移、真实控制、P3 容器、Toil 原子工作流、QGIS/Native 空间和可信静态制图全部通过。
- Release lock 精确包含 11 个 `0.12.1` wheel、三个 Catalog 和七个不可变镜像摘要。四个本轮重建镜像均携带相同源码摘要。

上述测试存在覆盖重叠。完整兼容 pytest 的 889 项已经包含 P7 定向 pytest；P1 的 13 项独立规则不在该 pytest 集合中。因此非重复门禁至少 902 项，均无失败、错误或跳过。

## 问题处理

- 首次 P7 定向联合回归出现一次 `table-validate` 错误分类。原因是此前人工中止旧镜像试跑时留下 6 个孤立测试 Worker 和 1 个测试 PostgreSQL 容器，旧 Worker 继续消费其测试环境。清理本轮专用残留后，失败用例单独通过，68 项联合回归也全部通过；生产代码未为此增加重试或容错分支。
- 首次全量回归发现 Platform Core 为提交前字段校验直接导入 `platform_contracts.science.TableFilterParameters`，违反核心包仅依赖标准库的架构边界。已改为 Core 内部纯标准库的字段引用核对；contracts 继续负责 HTTP 形状，Execution 和算子负责完整二次验证。架构 46 项定向测试和 889 项全量回归均通过。
- 每次源码变化后均重新构建 operator、checker、GDAL checker 和 tabular 镜像，并重新生成 wheel、Catalog、源码清单和 release lock，避免测试镜像与最终发行身份错配。

## 范围与限制

本轮没有数据库迁移、公开路由或页面改动。Filter 不修改字段、类型和值，不排序、去重、填补、转换、聚合、连接或换算单位。字符串有序比较使用 Unicode 码点顺序，不提供语言排序。没有新增 CLAIM_SUPPORT；要求该检查类型的 Filter 验收仍为 `INDETERMINATE`。checker 证明冻结输入和结果按固定谓词一致，不证明字段业务语义或现实数据真实性。

## 发行身份

- Operator Catalog：`sha256:b14ed8ef6b191c8af8c5807a9b1644d22a4a1b6154abd1b354968058a625e97b`
- Checker Catalog：`sha256:cc8362f7465712c9c8a42357679a3a7a9093f21f452bf775e04ee9346c03daa0`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- 镜像源码：`sha256:2ff1756c274717b197159dc986a9d96e33fedc06e62abede2bfea626a9edbca5`
- Release source tree：`sha256:9b56adc0134065f93ccd8cfc95311825d64993c25f52daa5d88865eadbbc862e`
- Release：`sha256:916708e5c28c0a7b4d840585fe2ae7bd181fc7446fc74d78e71fb6ed688cb959`

完整结构化证据见 `p72b-release-evidence.json`；目录、源码和发行冻结文件见同目录下的 `p72b-*-catalog.lock.json`、`p72b-source-manifest.json` 和 `p72b-release-lock.json`。

## 阶段结论

P7.2b 全部门禁通过，发布版本为 `0.12.1`。Filter 的契约、直接执行、Agent VERIFIED、派生工作流、预算、恢复、原子发布、独立复算和旧阶段兼容均已验证。本轮结束，不在同一轮实现 Cast、Join 或 Aggregate。
