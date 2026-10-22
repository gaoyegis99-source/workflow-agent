# P7.3a / 0.13.0 受限表格聚合协议

P7.3a 新增审核算子 `table-aggregate@1.0.0`。它只接收一个精确 `TableSelection`，复用现有 `TABULAR_V1`、Action、预算、Execution、固定容器、工作流、原子发布和 provenance 协议。它没有引入多输入身份，也没有实现 Join。

算子在 Operator Catalog 中标记为 `CURATED_PACKAGE / VERIFIED / ACTIVE`，固定实现为 `table-aggregate-native-v1`。客户端只能提交目录登记的算子、版本和参数，不能选择镜像、命令、表达式或资源上限。

## 参数契约

```json
{
  "semantics": "strict-table-aggregate-v1",
  "group_by": ["f0"],
  "null_key_policy": "GROUP",
  "measures": [
    {"measure_id": "rows", "operation": "COUNT_ROWS"},
    {"measure_id": "mean_level", "operation": "MEAN", "field_id": "f1"}
  ]
}
```

- `group_by` 包含 0–16 个不同字段，只允许 string、int64 和 bool。double 首期不能作为分组键，避免 NaN、负零和浮点等价关系造成不透明分组。
- `measures` 包含 1–64 个不同 `measure_id`。
- `COUNT_ROWS` 不携带字段；其他操作必须精确引用一个字段。
- `SUM`、`MEAN` 只接受 int64 或 double。`MIN`、`MAX`、`COUNT_NON_NULL` 和 `COUNT_DISTINCT` 支持四种平面标量类型。
- `display_name` 仅用于输出字段名称，必须是 1–200 字符的 NFC 文本，不能包含控制、格式控制或代理字符。

不支持 median、标准差、分位数、first/last、字符串拼接、任意表达式、HAVING、窗口、时间分桶或自动单位解释。

## Null、空输入和顺序

`null_key_policy` 固定为：

| 策略 | 行为 |
| --- | --- |
| `GROUP` | 含 null 的键作为明确分组参与计算 |
| `DROP` | 含任一 null 分组键的行不参与聚合，并记录丢弃数量 |
| `ERROR` | 以 `AGGREGATION_FAILED / NULL_GROUP_KEY` 结束，不发布成果 |

measure 中的 null 始终忽略。`COUNT_ROWS` 统计组内全部参与行；`COUNT_NON_NULL` 和 `COUNT_DISTINCT` 只统计非 null 值。没有 group key 的空输入产生一个全局组：三个 count 操作为 0，其他结果为 null。有 group key 的空输入产生 0 行。

分组顺序固定为各键在源表中的首次出现顺序；系统不按键值排序。measure 和输出列顺序与冻结请求一致，因此结果摘要可重复计算。

## 数值规则和失败

- 参与计算的 double measure 出现 NaN 或正负无穷时，执行以 `NONFINITE_VALUE` 失败。
- int64 `SUM` 使用无界中间整数并检查最终结果；超出 int64 时以 `NUMERIC_OVERFLOW` 失败。
- double `SUM` 和生产 `MEAN` 使用 `math.fsum`；int64 `MEAN` 使用 Decimal 中间计算，输出为有限 double。
- 任一聚合失败使整个执行为 `FAILED`，已开始的计算消耗一次额度，不发布资产。
- 失败诊断只保存 scope、measure ID、字段 ID、类别和计数，不保存原始值、样本或行号。

## 输出与三层校验

成功输出固定为 `report`、`table`、`method`：

- `report`：`table-aggregate-report-v1`，保存冻结参数、参与/丢弃行数、分组数量、输出字段和 measure 汇总；
- `table`：`table-aggregate-table-v1`，字段稳定命名为 `f0...`；
- `method`：`tabular-method-v1`，冻结分组顺序、null 规则和数值算法。

Platform 在写入 Action、预算和 outbox 前用标准库核对字段及操作适用性。Execution 在容器启动前按 Data 的受信表格描述再次核对，并在收集阶段检查文件集合、Schema、报告计数、字段来源、方法和有限数值。容器自身最后验证完整请求。三个层次均不猜测字段业务含义。

## 独立复算、Agent 和工作流

`tabular-aggregate-reproduction-v1` 位于 `groundwater-checker-pack`，不导入 `platform_tabular_operators`。它单独实现分组和全部七种操作；数值参考使用 Decimal，并以绝对 `1e-10`、相对 `1e-8` 核对 double。它还检查源表示摘要、字段顺序、report/table/method、Schema、目录身份和 provenance。

能力只匹配 `table-aggregate@1.0.0` 的 `report` 端口和 `table-aggregate-report-v1`。未知算子、版本、端口或 Schema 保持 `UNCOVERED / INDETERMINATE`，不能通过通用 checker 得到 PASS。

Agent v2 只能执行 TaskSpec 中冻结的 Aggregate 意图。一次直接 VERIFIED 闭环消耗三次额度：Aggregate、运行完整性和独立复算各一次。

冻结示例 `table-filter-aggregate-profile-v1` 执行：

```text
源表格 → Filter → Aggregate → Profile → 整组原子发布
```

三个节点各结算一次。派生绑定来自上游节点已核验的私有 manifest；任一节点失败或取消时不公开半组成果。

## 边界

本轮没有数据库迁移、公开路由、页面或 CLAIM_SUPPORT。聚合结果不自动继承单位或业务语义；正式科学结论仍需分析登记、语义修订和匹配的领域策略。Join 仍未实现。Join 之前必须先建设多输入精确身份、逐输入授权、派生来源映射、输出放大上限和独立双输入 checker 协议。

