# P7.2c / 0.12.2 严格表格类型转换协议

P7.2c 新增审核算子 `table-cast@1.0.0`。它只转换明确列出的平面标量字段，保持字段 ID、显示名称、字段顺序、行顺序和行数。算子不删除行、不修剪字符串、不解析时间、不换算单位，也不提供任意表达式。

算子在 Operator Catalog 中标记为 `CURATED_PACKAGE / VERIFIED / ACTIVE`，固定实现为 `table-cast-native-v1`，复用 `TABULAR_V1`、统一 Action、预算、Execution、固定容器、原子发布和 provenance 协议。

## 请求与失败策略

```json
{
  "semantics": "strict-scalar-cast-v1",
  "casts": [
    {
      "field_id": "f1",
      "target_type": "double",
      "on_error": "ERROR"
    }
  ]
}
```

一次请求包含 1–128 个不同字段。源和目标类型只能是 `string`、`int64`、`double`、`bool`，且必须不同。null 始终保持 null。

- `ERROR`：任一非 null 值无法转换时，整个执行以 `CAST_FAILED` 失败；已开始的计算消耗一次额度，不发布资产。
- `SET_NULL`：无法转换的值变为 null，执行可以成功。报告明确记录每个字段的输入 null、转换成功、失败和输出 null 数量。

失败诊断只包含字段 ID、源/目标类型和计数，不保存原始值、样本或行号。

## 严格转换矩阵

| 目标 | 允许的源和值 |
| --- | --- |
| `string` | bool 转为 `true/false`；int64 转为规范十进制；有限 double 转为规范 JSON 数值文本 |
| `int64` | 规范十进制字符串；有限、整数且在 int64 范围内的 double；bool 转为 0/1 |
| `double` | 有限 JSON 数值字符串；能被 IEEE-754 double 精确表示的 int64；bool 转为 0.0/1.0 |
| `bool` | 字符串只能是 `true/false`；int64 只能是 0/1；有限 double 只能是 0.0/1.0 |

因此 `01`、`+1`、`1.0` 不能转为 int64；NaN/Inf 不能转为字符串或整数；`2^53+1` 不能无损转为 double；`2^63` double 不能转为 int64。系统不做静默截断、舍入或格式猜测。

## 三层准入与输出核验

Platform 在写 Action、预算和 outbox 前，用仅依赖标准库的逻辑核对字段存在、源/目标不同、失败策略及重复字段。Execution 在直接执行或工作流节点创建容器前，使用 Data 返回的受信 Schema 再次核对。独立算子容器最后验证完整请求。

成功输出固定为 `report`、`table`、`method`：

- `report` 使用 `table-cast-report-v1`，记录参数、行数及逐字段计数；
- `table` 使用 `table-cast-table-v1`，字段顺序和行数与源表一致；
- `method` 使用 `tabular-method-v1`，冻结 `strict-scalar-cast-v1`。

Execution 独立验证输出文件集合、方法、参数、字段类型、行数、null 计数守恒和来源摘要，再允许 Data 发布 `OPERATOR_RESULT`。`ERROR` 失败只接受固定退出码 11 和受限诊断结构，不能伪装成成功成果。

## 独立复算与 Agent

`tabular-cast-reproduction-v1` 位于 `groundwater-checker-pack`，不导入 `platform_tabular_operators`。它独立实现转换矩阵，逐值核对输出，并验证 report、method、Schema、manifest 和 provenance。能力只匹配 `table-cast@1.0.0` 的 `report` 端口及 `table-cast-report-v1`；其他算子、版本、端口或 Schema 保持 `UNCOVERED / INDETERMINATE`。

Agent v2 只能提交 TaskSpec 中冻结的 Cast 意图，不能改变资产、字段、目标类型、失败策略、实现或资源。一次 VERIFIED 直接闭环消耗 3 次额度：一次 Cast、一次运行完整性检查、一次独立复算。

## 工作流

冻结示例 `table-project-filter-cast-profile-v1` 执行：

```text
源表格 → Project → Filter → Cast → Profile → 整组原子发布
```

四个节点各消耗一次额度。Cast 的派生输入来自 Filter 已核验的私有 manifest；Profile 使用 Cast 的新字段类型。任一节点失败或取消，所有兄弟成果都保持私有，不形成半组资产。

## 边界

P7.2c 不增加数据库迁移、公开路由或页面，不增加 CLAIM_SUPPORT。它不实现时间解析、单位转换、`DROP_ROW`、Join、Aggregate 或用户表达式。现实数据语义和字段业务含义仍需 Data 语义修订、研究协议和领域策略约束。
