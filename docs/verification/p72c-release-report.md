# P7.2c / 0.12.2 严格表格类型转换闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增审核算子 `table-cast@1.0.0` 和实现 `table-cast-native-v1`。仅支持 string、int64、double、bool 间明确的严格转换，保持字段身份、字段顺序、行顺序和行数。
- 冻结 `strict-scalar-cast-v1`。不修剪字符串、不截断或舍入、不解析时间、不换算单位；数值溢出、非整数 double、NaN/Inf、非规范字符串和有损 int64→double 均为转换失败。
- 每个字段显式选择 `ERROR` 或 `SET_NULL`。`ERROR` 使用退出码 11 和受限 `CAST_FAILED` 诊断，消耗已开始的执行额度且不发布资产；`SET_NULL` 将失败值转为 null 并在报告中保存计数。
- Platform 在持久写入前以纯标准库核对受信字段；Execution 在容器启动前再次核对；算子容器验证完整请求。Execution 对成功输出执行 Schema、字段、行数、null 计数、方法和来源检查。
- 新增独立 `tabular-cast-reproduction-v1`，不导入生产算子，逐值复算严格转换并核对 report/table/method/manifest/provenance。能力精确匹配 Cast 的 report 和 `table-cast-report-v1`。
- Agent v2 可执行冻结 Cast 意图，经运行完整性和领域复算形成 VERIFIED Completion；直接闭环使用 3 次执行额度。
- 新增 `Project → Filter → Cast → Profile` 四节点真实 Toil 工作流。每个节点各结算一次，全部成功后整组原子发布；任何节点失败时不公开半组成果。
- 11 个 Python 包和 CWL runtime 统一为 `0.12.2`。Operator Catalog 为 10 个算子，Checker Catalog 为 10 个能力；数据库 schema 和公开路由不变。

## 测试结果

- `bash scripts/check_p7.sh --phase table-cast`：13 项通过，0 失败、0 错误、0 跳过，耗时 68.80 秒；使用真实 PostgreSQL、HTTP、Docker、Agent、两个 checker 和四节点 Toil 工作流。
- P7.0–P7.2c 定向组合回归：106 项通过，0 失败，耗时 341.01 秒。
- `bash scripts/check_p7.sh --phase compatibility`：902 项通过，0 失败、0 错误、0 跳过，耗时 2335.39 秒（38 分 55 秒）。
- `bash scripts/check_p7.sh --phase catalog`：P7 目录 24 项通过；P1 规则 13 项通过；Ruff 和格式检查通过。
- 架构与 Cast 定向测试：43 项通过，验证 Platform Core 只依赖标准库及 checker 不导入生产 tabular 包。
- `uv run --locked python scripts/verify_distribution.py`：11 个 wheel 和源码包构建成功；仓库外锁定安装、核心标准库边界、API/OpenAPI/停机、四服务迁移、P3 容器、Toil 原子工作流、QGIS/Native 空间与可信静态制图全部通过。
- Release lock 精确包含 11 个 `0.12.2` wheel、三个 Catalog 和七个不可变镜像摘要。统一阶段入口复建后的 operator、checker、GDAL checker 和 tabular 镜像摘要与冻结值一致。

上述测试存在覆盖重叠。完整兼容 pytest 的 902 项已包含 P7 定向 pytest；P1 的 13 项独立规则不在该集合中。因此非重复门禁至少 915 项，均无失败、错误或跳过。

## 问题处理

- 修复 double→int64 上界检查：先把有限整数 double 转为 Python 整数，再按 int64 范围判断，避免 `2^63-1` 在 double 中舍入为 `2^63` 后被错误接受。
- 固定报告中的 cast 汇总顺序为请求顺序，而不是源表字段顺序，保证参数与结果一一对应且摘要可复现。
- Parquet 输出检查增加逐列 null 数，Execution 由此独立验证 `output_null_count = input_null_count + failure_count`，防止伪造 SET_NULL 报告。
- Cast 前置字段核对继续位于 Platform Core 的纯标准库函数中，避免重现 P7.2b 曾发现的核心包依赖 contracts 问题。

## 范围与限制

本轮没有数据库迁移、公开路由、页面或 CLAIM_SUPPORT。Cast 不改变行数，不支持时间和空间类型、单位转换、字符串修剪、默认值、删除失败行或任意表达式。checker 证明冻结输入与结果符合严格转换协议，不证明字段业务语义或现实观测真实性。

## 发行身份

- Operator Catalog：`sha256:395364eeb8a6b8ff42f066d1ed893efc16ac9b468821bd175721db588f8b106a`
- Checker Catalog：`sha256:3b16a17a0b4910e311b8b3fb4ee9c31b6b3d1ccfc12ff55a48df50224eced1e9`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- 镜像源码：`sha256:51b84f9753d7b3d81f933e25ca36a3f70cd9256cde57b0197e0796503e9e6712`
- Release source tree：`sha256:3762cc5254b1f1d9a2ed54fe587a1b33c4338d1aadabca96bbeb98af5c9425ce`
- Release：`sha256:47e57a0565f0dce9e9389d2eab54d479d1cb5dd9a58a5fb287de7375da3d1fbe`

完整结构化证据见 `p72c-release-evidence.json`；目录、源码和发行冻结文件见同目录下的 `p72c-*-catalog.lock.json`、`p72c-source-manifest.json` 和 `p72c-release-lock.json`。

## 阶段结论

P7.2c 全部门禁通过，发布版本为 `0.12.2`。Cast 的严格契约、直接执行、失败不发布、Agent VERIFIED、派生工作流、预算、恢复、原子发布、独立复算和旧阶段兼容均已验证。本轮结束，不在同一轮实现 Join 或 Aggregate。
