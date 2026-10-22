# P7.1b 表格算子可信验收协议

P7.1b 为 `table-profile@1.0.0` 和 `table-validate@1.0.0` 增加两项固定检查能力。计算算子仍在 `groundwater-tabular-operator-pack` 中运行；独立复算位于 `groundwater-checker-pack`，后者不能导入或安装生产表格算子包。

## 能力匹配

| Capability | 算子 | 端口 | Schema | 检查类型 |
|---|---|---|---|---|
| `tabular-profile-reproduction-v1` | `table-profile@1.0.0` | `report` | `table-profile-report-v1` | `DATA_VALIDITY`、`RESULT_REPRODUCTION` |
| `tabular-validation-reproduction-v1` | `table-validate@1.0.0` | `report` | `table-validation-report-v1` | `DATA_VALIDITY`、`RESULT_REPRODUCTION` |

匹配同时检查结果类型、算子、版本、输出端口和 Schema。任一字段不同都生成 `UNCOVERED / INDETERMINATE`，通用运行完整性检查不能代替领域复算。表格结果没有 `CLAIM_SUPPORT` 能力。

## 输入和隔离

Execution 从最终发布 manifest 取得三种成果表示、精确源表、冻结参数、实现身份和 provenance。每个文件在进入 checker 私有只读目录前重新核对项目、AssetRef、表示身份、大小和 SHA-256。工作流结果从最终 publication member 反查节点账本；派生表格从原节点的私有、已核验输出读取，不根据文件名或扩展名猜测来源。

Checker 用 PyArrow 读取 Parquet，但统计和规则算法独立实现。Profile 复算字段顺序、类型、空值、不同值、数值范围、Unicode 长度和布尔计数；Validation 重新执行六类规则。报告 JSON、结果 Parquet、method、manifest 和 provenance 必须相互一致。

## 编排和预算

运行完整性先执行，领域复算随后执行。一个领域 checker run 同时覆盖 `DATA_VALIDITY` 与 `RESULT_REPRODUCTION`，但逐项返回结果。单算子闭环消耗三次额度；同时验收 Profile 和 Validation 时由一个运行完整性检查和两个领域复算组成，共消耗五次额度。

`VALIDATION_FAILED` 保持失败执行，没有发布资产，也不能注册 Deliverable。VERIFIED 只有全部必要 CoverageItem 为 PASS 时才能完成。响应重放、Worker 接管和 ERROR 重试继续复用原 Action、checker run 和预算记录。
