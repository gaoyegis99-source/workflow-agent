# P7.2a / 0.12.0 表格投影算子协议

P7.2a 新增审核算子 `table-project@1.0.0`。它只负责选择字段、调整字段顺序和修改显示名称，不过滤行、不转换类型，也不改变单元格值。算子在 Operator Catalog 中标记为 `CURATED_PACKAGE / VERIFIED / ACTIVE`，继续使用 P7 的统一 Action、预算、Execution、容器、原子发布和 provenance 协议。

## 参数与字段身份

请求必须显式给出 1–128 个不重复的源字段：

```json
{
  "fields": [
    {"source_field_id": "f2", "display_name": "水位"},
    {"source_field_id": "f0", "display_name": "监测点"}
  ]
}
```

`source_field_id` 必须存在于受信输入表格描述中。`display_name` 省略时沿用源显示名称；提供时必须去除首尾空白后仍非空、为 Unicode NFC，并且不含控制字符、格式控制字符或代理字符。显示名称是元数据，不承担字段身份。

输出按请求顺序重新分配稳定字段 ID `f0`、`f1`……。例如源 `f2` 成为输出 `f0`，源 `f0` 成为输出 `f1`。`report` 明确保存每个输出字段的序号、输出字段 ID、源字段 ID、源名称、输出名称和物理类型，因此重排和重命名均可复核。

## 值与 Schema 语义

算子保持：

- 行数和行顺序；
- 物理类型；
- null；
- NaN、正负无穷；
- `-0.0` 与 `0.0` 的具体浮点表示；
- 字符串和布尔值。

算子不执行过滤、去重、填补、聚合、类型转换或单位换算。输出固定为：

```text
report.json
table.parquet
method.json
```

成果使用 `OPERATOR_RESULT`，对应 Schema 为 `table-project-report-v1`、`table-project-table-v1` 和 `tabular-method-v1`。成果 manifest 保存源资产、源表示摘要、参数摘要、字段映射、算子/实现/镜像/目录摘要、Action 和 Execution 身份。

## 独立复算

`tabular-project-reproduction-v1` 位于 `groundwater-checker-pack`，不导入 `platform_tabular_operators`。它重新读取源 Parquet 和已发布的投影 Parquet，逐项检查：

- 字段选择和顺序；
- 源字段到输出字段的身份映射；
- 显示名称和物理类型；
- report、table、method 和 manifest 的一致性；
- 每一行每一列的值。

double 比较将所有 NaN 视为同一表格语义值；非 NaN 值按 IEEE 754 位模式核对，因此能够识别符号零变化。能力精确匹配 `table-project@1.0.0`、`report` 端口和 `table-project-report-v1`。未知版本、端口或 Schema 保持 `UNCOVERED / INDETERMINATE`。

直接算子 VERIFIED 闭环需要一次计算、一次运行完整性检查和一次领域复算，最少消耗 3 次执行额度。P7.2a 没有增加 CLAIM_SUPPORT 能力。

## 工作流衔接

冻结示例 `table-project-profile-v1` 执行：

```text
源表格 → table-project → table-profile → 整组原子发布
```

派生绑定必须从上游节点已核验的 manifest 和表示摘要构造。表格描述允许位于根描述或 `manifest.tabular`，两个位置经统一适配后具有相同语义。中间投影结果在父工作流提交前保持私有；任一节点失败时不得公开半组成果。

## 边界

P7.2a 不增加公开路由、页面功能或数据库迁移，也不实现行过滤、类型转换、连接和聚合。后续 `table-filter` 与 `table-cast` 必须使用独立版本、独立参数契约和独立复算能力，不能通过扩展 `table-project@1.0.0` 改变其既有语义。
