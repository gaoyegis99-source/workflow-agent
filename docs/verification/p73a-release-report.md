# P7.3a / 0.13.0 受限表格聚合闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增审核算子 `table-aggregate@1.0.0` 和实现 `table-aggregate-native-v1`。它只接收单一精确 `TableSelection`，没有引入 Join 或多输入协议。
- 冻结 `strict-table-aggregate-v1`，支持 `COUNT_ROWS`、`COUNT_NON_NULL`、`COUNT_DISTINCT`、`MIN`、`MAX`、`SUM` 和 `MEAN`。分组字段最多 16 个，measure 最多 64 个。
- 明确 `GROUP / DROP / ERROR` 三种 null key 策略、空输入规则、null measure 忽略规则和首次出现分组顺序。double group key 被拒绝。
- int64 `SUM` 做溢出检查；所有被选 double measure 遇到 NaN/Inf 均失败；失败使用退出码 12 和受限 `AGGREGATION_FAILED` 诊断，已开始的计算计费且不发布资产。
- Platform 在 Action/预算/outbox 写入前核对受信字段；Execution 在容器启动前再次核对，并在收集阶段验证 report/table/method、Schema、行数、字段来源、方法和有限输出。
- 新增独立 `tabular-aggregate-reproduction-v1`。checker 不导入生产算子，使用单独实现和 Decimal 数值参考复算分组与七种操作，精确检查源摘要、输出、方法和 provenance。
- Agent v2 可以执行冻结 Aggregate 意图，并经运行完整性和领域复算形成 VERIFIED Completion；直接闭环使用 3 次执行额度。
- 新增 `Filter → Aggregate → Profile` 三节点真实 Toil 工作流。三个节点各结算一次，全部成功后整组原子发布；派生输入依据已核验的私有 manifest，而不是文件名推断。
- 11 个 Python 包和 CWL runtime 统一为 `0.13.0`。Operator Catalog 为 11 个算子，Checker Catalog 为 11 个能力；数据库 schema、公开路由和页面不变。

## 测试结果

- `bash scripts/check_p7.sh --phase table-aggregate`：17 项通过，0 失败、0 错误、0 跳过，最终冻结镜像运行耗时 64.54 秒。
- P7.0–P7.3a 定向组合回归：123 项通过，0 失败、0 错误、0 跳过，耗时 419.87 秒。
- `bash scripts/check_p7.sh --phase compatibility`：919 项通过，0 失败、0 错误、0 跳过，耗时 2411.10 秒（40 分 11 秒）。
- `bash scripts/check_p7.sh --phase catalog`：P7 目录 24 项通过；P1 规则 13 项通过；Ruff 与格式检查通过。
- 契约与目录定向回归：133 项通过，验证目录闭合、核心标准库边界和旧契约兼容。
- `uv run --locked python scripts/verify_distribution.py`：11 个 wheel 和 11 个源码包构建成功；仓库外锁定安装、核心标准库边界、API/OpenAPI/停机、四服务迁移、P3 容器、Toil 原子工作流、QGIS/Native 空间与可信静态制图全部通过。
- Release lock 精确包含 11 个 `0.13.0` wheel、三个 Catalog 和七个不可变镜像摘要。

上述 pytest 结果存在覆盖重叠。完整兼容的 919 项已包含 P7 定向测试；P1 的 13 项规则独立运行。因此非重复门禁至少 932 项，均无失败、错误或跳过。

## 问题处理

- 将分组键限定为 string、int64 和 bool，避免用隐含规则处理 double 的 NaN、负零与浮点等价关系。
- 将分组输出顺序固定为首次源行出现顺序，避免数据库、散列表或值排序差异改变成果摘要。
- 区分 global empty 与 grouped empty：前者产生一个 count=0 的结果行，后者产生零行；非 count measure 在空组中为 null。
- 生产 double 聚合使用 `math.fsum`，checker 使用 Decimal 独立参考并按固定容差比较，避免复制生产数值实现形成假独立验证。
- Aggregate 失败诊断只保留字段/measure 身份、类别和计数，不泄露原始值或行号。
- 将 P7.3a 测试目录纳入镜像源码身份和 release source identity，避免代码已发布而新门禁未进入冻结摘要。

## 范围与限制

本轮没有数据库迁移、公开路由、页面或 CLAIM_SUPPORT。Aggregate 不提供 median、标准差、分位数、任意表达式、HAVING、窗口、时间分桶或单位推断。checker 证明冻结输入与结果符合协议，不证明业务字段语义或现实观测真实性。

Join 没有实施。P7.3b 仅在出现明确真实用例时建设多输入精确身份、逐输入授权、派生来源映射和输出放大仲裁；完成这些基础后，才评估 P7.3c 的 INNER/LEFT、禁止多对多 Join 试点。

## 发行身份

- Operator Catalog：`sha256:51a6f5a4d32deac43fed343aa679c8f8c5be2f5b1707e06b0838997b27036e50`
- Checker Catalog：`sha256:65573abeeb0906e1cad9218efe9ddf4a9c7ffc2517eb054271a3383be5bc0e78`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- 镜像源码：`sha256:d3ea29751e74e69014d3423b28a7718fbbd6bb6f304acd375c30f7ab5169c3e6`
- Release source tree：`sha256:c1804ead3bca2b54fdd16f5e8313e60c0f01139e481d70f926613d940681b5cc`
- Release：`sha256:87f20e2a624a618f882c9b9f20095f0e3aff329919c5fdeb49ac6ed06f9845a7`

完整结构化证据见 `p73a-release-evidence.json`；目录、源码和发行冻结文件见同目录下的 `p73a-*-catalog.lock.json`、`p73a-source-manifest.json` 和 `p73a-release-lock.json`。

## 阶段结论

P7.3a 全部门禁通过，发布版本为 `0.13.0`。受限 Aggregate 的契约、直接执行、失败不发布、Agent VERIFIED、派生工作流、预算、恢复、原子发布、独立复算和旧阶段兼容均已验证。本轮结束，不在同一轮实施多输入基础或 Join。
