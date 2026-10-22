# P5-R2 可信检查器能力契约

P5-R2 使用部署生成的只读 `checker-catalog.json`。客户端只能提交验收请求，不能选择 capability、实现、镜像、命令或容差。Platform 按 `BoundOutputRef`、已发布 manifest、算子版本和 schema 精确匹配能力；Execution 只接受 CoveragePlan 中冻结的能力和镜像身份。

目录首批包含五项能力：

| Capability | 检查内容 | 运行镜像 |
|---|---|---|
| `platform-runtime-integrity-v1` | 角色、发布、摘要、schema 和来源身份 | 通用 checker |
| `p3-timeseries-reproduction-v1` | P3 输入有效性及 Decimal 独立复算 | 通用 checker |
| `spatial-vector-validity-v1` | GDAL 独立读取 CRS、范围、要素数和几何有效性 | GDAL checker |
| `cartographic-product-integrity-v1` | PNG 结构和像素、模板、来源空间事实及发布 manifest | 通用 checker |
| `p3-descriptive-claim-support-v1` | 已登记 P3 描述性标量断言 | 通用 checker |

CoveragePlan schema v3 冻结完整目录、目录摘要、每个 run 的 capability/实现/镜像/输入输出契约摘要、输入角色、计划项及依赖。运行完整性必须先通过；描述性主张还依赖对应的 P3 独立复算通过。

没有匹配能力时，计划项为 `UNCOVERED / INDETERMINATE`。EXPLORATORY 可带此限制完成，VERIFIED 不能完成。关联、预测和因果主张不由描述性 checker 代替。

一个 run 可以覆盖同一能力和同一输入包的多个计划项，但返回项集合必须完全相等。漏项、重复项、额外项、旧能力版本、错误 plan digest 或错误契约摘要均以 `INVALID_BINDING` 拒绝，且不会部分写入权威检查事实。
