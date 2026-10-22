# P7.2a / 0.12.0 表格投影闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增审核算子 `table-project@1.0.0`。它显式选择字段、按请求重排字段并修改显示名称；输出字段重新获得稳定 `f0...fN` 身份，行数、行序、物理类型、null、NaN/Inf、符号零和其他单元格值保持不变。
- `groundwater-tabular-operator-pack` 增加独立 CLI 实现 `table-project-native-v1`，继续使用固定 `TABULAR_V1`、`table-small-v1`、非 root、无网络、只读输入和受限输出环境。
- Execution 增加投影成果核验，固定 `report/table/method` 三个端口及 `table-project-report-v1`、`table-project-table-v1`、`tabular-method-v1` Schema。发布 manifest 保存源字段到输出字段的精确映射及完整算子、实现、目录、镜像、Action、Execution 和输入来源。
- 新增 `tabular-project-reproduction-v1`。checker 与生产算子包隔离，独立核对字段身份、顺序、名称、类型、report/method/provenance，并逐行逐列比较源表与输出表。
- Agent v2 可通过 TaskSpec 冻结 `table-project` 意图；模型不能修改字段、名称、输入、实现或资源。直接执行经过运行完整性和领域复算后能够形成 VERIFIED Completion，最少使用 3 次额度。
- 新增冻结工作流 `table-project-profile-v1`，真实验证“源表 → Project → Profile → 整组原子发布”。派生表格描述统一支持受信 `manifest.tabular` 形状，中间成果在父级提交前保持私有。
- 工作区 11 个 Python 发行包及 CWL runtime 统一为 `0.12.0`。Operator Catalog 现在包含 8 个算子；数据库 schema 保持不变。

## 测试结果

- `bash scripts/check_p7.sh --phase table-project`：11 项通过，0 失败、0 错误、0 跳过；使用真实 PostgreSQL、HTTP、Docker、Agent Worker、checker 和 Toil 工作流。
- P7.1a、P7.1b、P7.2a 联合定向回归：54 项通过，0 失败。
- `bash scripts/check_p7.sh --phase compatibility` 最终结果：874 项通过，0 失败、0 错误、0 跳过，耗时 37 分 44 秒。
- `bash scripts/check_p7.sh --phase catalog`：P7 目录 24 项通过，P1 规则 13 项通过；Ruff 和格式检查通过。
- `uv run --locked python scripts/verify_distribution.py`：11 个 wheel 和源码包构建成功；核心标准库边界、仓库外锁定依赖安装、API/OpenAPI/优雅停机、四服务迁移、真实任务控制、P3 容器、Toil 工作流、GeoPackage/QGIS/Native 空间与静态制图闭环全部通过。
- Release lock 精确包含 11 个 `0.12.0` wheel、Operator/Checker/Tool Catalog 和七个不可变镜像摘要。

上述测试存在覆盖重叠。按完整兼容 pytest 874 项和独立 P1 规则 13 项计算，非重复门禁至少 887 项，均无失败、错误或跳过。

## 问题处理

- 首次真实 Project→Profile 工作流发现，根输入的表格描述位于根对象，而派生成果位于 `manifest.tabular`。Execution runner 和 tabular CLI 已统一适配两种受信形状；修复后原工作流使用相同节点身份、预算和 publication 成功完成。
- 首次完整兼容回归为 873 通过、1 失败。失败来自 P5-R2 历史测试把 checker 总数硬编码为 7。测试现精确验证七项 P5-R2 基线能力仍完整存在，并验证所有当前 capability digest 唯一；P7.2a 测试单独验证新增第八项能力。定向复测和第二次完整回归均通过。
- 初次生成 release lock 时误用旧 Tool Catalog 阶段名 `science`，得到 `p6-science-tools-v1`。发布目录已改用 `p7-science`，锁定 `p7-science-tools-v2`，并重新生成和校验 release lock。
- 一处 tabular CLI 格式不符合 Ruff；已格式化并重新构建最终镜像。最终四个本轮相关镜像携带相同发布源码摘要。

## 范围与限制

本轮没有新增数据库迁移、公开路由或页面功能。`table-project` 不过滤行、不转换类型、不聚合、不连接、不去重、不填补缺失值，也不换算单位。没有新增 CLAIM_SUPPORT；要求该检查类型的投影结果仍为 `INDETERMINATE`。checker 证明受信输入与投影成果按冻结参数一致，不证明字段业务含义或现实数据真实性。

## 发行身份

- Operator Catalog：`sha256:1d4997dc9a1d953aacb67937ac7ca63d186bb6ac8bf5886491a969c4b0f3a763`
- Checker Catalog：`sha256:440ed98c511284f69d83ac831bbd1d6551f3f552e21db261ea63fc0494d27df0`
- Tool Catalog：`sha256:e84e26da95838af65a766286cce9626ebe39499a9fefa870e561936639a0388e`
- 镜像源码：`sha256:3dda0de99cafbd82aca95805f3adfb956ddcfb60d223b5c8be982121711973d2`
- Release source tree：`sha256:d2f773cb01ca387186a53d6d5b3c46ee3a1dfc0b0d999c26cda725cfc860efe2`
- Release：`sha256:8cc00e70e67848d2f207192c26d89a2f958c10fdfc840fcce5efb6b8789a89c9`

完整结构化证据见 `p72a-release-evidence.json`；目录、源码和发行冻结文件见同目录下的 `p72a-*-catalog.lock.json`、`p72a-source-manifest.json` 和 `p72a-release-lock.json`。

## 阶段结论

P7.2a 全部门禁通过，发布版本为 `0.12.0`。Project 的直接执行、Agent VERIFIED、派生表格工作流、恢复、原子发布和旧阶段兼容均已验证。本轮结束，不在同一轮实现 Filter 或 Cast。
