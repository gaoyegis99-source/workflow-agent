# P7.2b / 0.12.1 表格过滤算子协议

P7.2b 新增审核算子 `table-filter@1.0.0`。它按冻结谓词选择行，保持字段身份、显示名称、物理类型、列顺序和被保留行的原始顺序。算子不修改单元格值，不执行类型转换、排序、去重、填补、聚合或单位换算。

算子在 Operator Catalog 中标记为 `CURATED_PACKAGE / VERIFIED / ACTIVE`，实现为 `table-filter-native-v1`，继续使用统一 Action、预算、Execution、固定容器、原子发布和 provenance 协议。

## 有界谓词

请求使用拓扑排序的节点表，而不是递归 JSON 或字符串表达式：

```json
{
  "predicate": {
    "semantics": "kleene-three-valued-v1",
    "root_id": "valid_positive",
    "nodes": [
      {
        "node_id": "positive",
        "kind": "COMPARE",
        "field_id": "f1",
        "operator": "GT",
        "value_type": "double",
        "value": "1.5",
        "values": null,
        "arguments": []
      },
      {
        "node_id": "finite",
        "kind": "IS_FINITE",
        "field_id": "f1",
        "operator": null,
        "value_type": null,
        "value": null,
        "values": null,
        "arguments": []
      },
      {
        "node_id": "valid_positive",
        "kind": "AND",
        "field_id": null,
        "operator": null,
        "value_type": null,
        "value": null,
        "values": null,
        "arguments": ["positive", "finite"]
      }
    ]
  }
}
```

支持 `COMPARE`、`IN`、`IS_NULL`、`IS_NAN`、`IS_FINITE`、`NOT`、`AND` 和 `OR`。比较操作为 `EQ`、`NE`、`LT`、`LE`、`GT`、`GE`；布尔字段只允许 `EQ` 和 `NE`。`IS_NAN` 与 `IS_FINITE` 只适用于 double 字段。

谓词最多 128 个节点、深度最多 8；AND/OR 每个节点包含 2–16 个参数。节点只能引用此前定义的节点，根节点必须最后出现，非根节点必须恰好被引用一次，因此结构是一棵确定的有向树。`IN` 最多 64 个规范值且语义值不能重复。字段和值类型在 Action、预算和 outbox 写入前按 Data 返回的受信 Schema 核对；工作流派生输入在 Execution 创建容器前再次核对。

用户不能提交 SQL、正则表达式、Python、JavaScript、函数名、镜像、命令或任意表达式语言。

## 三值逻辑

谓词使用固定的 Kleene 三值逻辑：

| 操作 | 规则 |
| --- | --- |
| 普通比较 | null 或 NaN 参与时为 `UNKNOWN` |
| `IN` | null 或 NaN 为 `UNKNOWN`；其他值按字段类型精确比较 |
| `IS_NULL` | 对所有行产生 `TRUE` 或 `FALSE` |
| `IS_NAN` / `IS_FINITE` | null 为 `UNKNOWN` |
| `NOT UNKNOWN` | `UNKNOWN` |
| `AND` | 存在 `FALSE` 即 `FALSE`；否则存在 `UNKNOWN` 即 `UNKNOWN` |
| `OR` | 存在 `TRUE` 即 `TRUE`；否则存在 `UNKNOWN` 即 `UNKNOWN` |

只有根谓词为 `TRUE` 的行被保留。`FALSE` 和 `UNKNOWN` 都不进入结果，避免 `NOT(field > 0)` 意外选中 null 或 NaN。需要选择这些值时必须显式使用 `IS_NULL` 或 `IS_NAN`。

int64 使用规范十进制并检查 64 位范围；double 使用有限 JSON 数值文本；bool 只接受 `true` 或 `false`；string 必须为 Unicode NFC。字符串顺序比较使用 Unicode 码点顺序，不声明语言排序语义。

## 成果与复算

成功输出固定为 `report`、`table` 和 `method`：

- `report` 记录输入/输出/丢弃行数、TRUE/FALSE/UNKNOWN 数量、参数和规范谓词摘要；
- `table` 保存过滤后的 Parquet，允许零行；
- `method` 冻结算子版本、源摘要、方法和三值逻辑版本。

对应 Schema 为 `table-filter-report-v1`、`table-filter-table-v1` 和 `tabular-method-v1`。Execution 独立检查文件集合、Schema、字段身份、行数守恒、计数守恒、参数摘要、方法和来源，再允许 Data 发布 `OPERATOR_RESULT`。

`tabular-filter-reproduction-v1` 位于 `groundwater-checker-pack`，不导入 `platform_tabular_operators`。它独立解析谓词和重算每一行的三值结果，逐列核对被保留行，并验证 report、method、manifest 和 provenance。能力只匹配 `table-filter@1.0.0` 的 `report` 端口及 `table-filter-report-v1`；其他算子、版本、端口或 Schema 保持 `UNCOVERED / INDETERMINATE`。

直接 VERIFIED 闭环需要一次过滤计算、一次运行完整性检查和一次领域复算，最少消耗 3 次执行额度。本轮没有增加 CLAIM_SUPPORT 能力。

## 工作流衔接

冻结示例 `table-project-filter-profile-v1` 执行：

```text
源表格 → table-project → table-filter → table-profile → 整组原子发布
```

三个节点各消耗一次执行额度。派生绑定只接受上游已核验 manifest 和表示摘要。中间 Project 与 Filter 表在父工作流提交前保持私有；任一节点失败或取消时不得公开半组成果。

## 边界

P7.2b 不增加公开路由、页面或数据库迁移。它不实现类型转换、Join、Aggregate、任意表达式或自然语言条件。后续 `table-cast` 使用独立算子版本、失败策略和复算能力，不能扩展 `table-filter@1.0.0` 改变现有语义。
