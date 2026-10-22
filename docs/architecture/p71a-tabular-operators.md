# P7.1a 独立表格算子协议

P7.1a 用两个小而完整的表格算子验证独立包边界。它们在 Operator Catalog 中属于 `CURATED_PACKAGE / VERIFIED / ACTIVE`，但与平台算子一样通过 Action、预算、Execution、固定容器、原子发布和 provenance 运行。系统工具和 checker 仍使用各自目录，不能从公开 `/executions` 解析。

## 输入与运行边界

`TableSelection` 精确绑定 AssetRef、表示 ID 和 SHA-256。Data `/resolve` 只接受已经发布且具备受信表格描述的 Parquet，字段类型限于 `string`、`int64`、`double`、`bool`，最多 100,000 行和 128 列。根资产和工作流私有派生成果使用相同的 `table` 端口；Execution 按受信描述暂存为只读 `table.parquet`，不从扩展名猜测类型。

目录登记 `TABULAR_V1` 和 `table-small-v1`：1 CPU、1 GiB、64 个进程、120 秒、100 MiB 输出。镜像以非 root 用户运行，无网络、只读根目录、只读输入和独立输出目录。客户端不能提供镜像、命令、协议或资源上限。

## 算子

`table-profile@1.0.0` 要求显式选择 1–128 个不重复字段。它输出字段身份、物理类型、行数、空值/非空值/精确不同值数量，以及按类型限定的数值范围、字符串长度或布尔计数。NaN 和正负无穷参与精确不同值计数，同时单独计入非有限值数量。成果不包含样本、原值或高频值。

`table-validate@1.0.0` 支持 `ROW_COUNT`、`TYPE`、`NOT_NULL`、`UNIQUE`、`RANGE` 和 `ALLOWED_VALUES`。空值键不参与 `UNIQUE`；NaN 作为稳定值比较，正负无穷分别比较；`RANGE` 遇到 NaN/Inf 必然失败。整数使用规范十进制，double 使用有限 JSON 数值文本，字符串要求 NFC。

验证全部通过时发布 `report`、`table`、`method`。任一规则失败时固定退出码 10，Execution 记录 `FAILED / VALIDATION_FAILED`，只保留规则 ID、类型和失败数量，不发布资产。输入或协议错误使用 `INPUT_ERROR` 或 `INVALID_BINDING`。

## 成果和工作流

成功成果使用显式 `OPERATOR_RESULT` 描述符，固定三种表示，保存精确算子、实现、目录、镜像、Action、Execution、输入摘要、参数摘要和表格 Schema。Execution 的 Parquet 适配器检查物理 Schema；应用层只依赖注入的检查端口。额外文件、缺失文件、符号链接、错误摘要、错误 Schema 和请求身份不一致均拒绝。

冻结示例覆盖：单 Profile、单 Validate、两者并行，以及 P3 完整率结果进入 Profile。工作流派生绑定来自已核验节点 manifest。任一 Validate 失败，父工作流失败，成功兄弟节点继续是私有恢复材料，Data 不公开半组资产。

## Agent v2

TaskSpec 最多冻结 8 个 `agent_operator_intents`。每项含唯一算子版本、精确 TableSelection 和完整参数。新 AgentRun 的 `execution.submit_operator@2.0.0` 只提交目录中的算子身份；后端从 TaskSpec 取得资产、规则和参数。模型不能修改它们，也不能选择实现、镜像或资源。v1 工具只用于封存的旧 AgentRun。

