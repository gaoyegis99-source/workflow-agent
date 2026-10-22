# P5.2 研究协议、来源与策略契约

P5.2 在 Platform 中增加不可变的研究记录。Data、Execution 和 Knowledge 仍分别拥有资产、计算和正文事实；Research 只保存精确引用、读取或执行观察、协议版本和策略决定，不复制其他服务的业务状态。新增修订会读取 EvidenceRef 的当前生命周期；撤回正文仍可历史读取，但不能写入新的研究或主张修订。

令 `P=/api/v1/projects/{project_id}`。新增接口如下：

| 接口 | 权限 | 行为 |
| --- | --- | --- |
| `POST P/tasks/{task_id}/analysis-revisions` | EDITOR | 创建不可变探索修订；所有外部引用先按精确版本读取 |
| `POST P/tasks/{task_id}/analysis-revisions/{revision_id}/register` | EDITOR | 冻结正式登记和登记前观察水位 |
| `POST P/tasks/{task_id}/claims` | EDITOR | 创建不可变、限定对象和范围的主张修订 |
| `GET P/tasks/{task_id}/research` | VIEWER | 返回协议、登记、主张、来源边、策略决定和缺项 |

所有写入都需要 `Idempotency-Key`。相同键和内容返回原对象；相同键不同内容返回 `409 IDEMPOTENCY_CONFLICT`。任务要求修订、项目身份和当前角色会在写事务内重新检查。

## 协议和登记

`AnalysisRevision` 保存问题、研究对象、时空范围、输入角色、方法、指标、比较规则、数据划分、停止规则、允许偏离、检查器版本和可选的地下水比较事实。保存后不能更新；变更必须创建带 `supersedes_revision_id` 和原因的新修订。查询时状态由不可变事实推导为 `EXPLORATORY`、`REGISTERED` 或 `SUPERSEDED`。

登记只允许当前任务要求修订上的最新分析修订。`observation_watermark` 冻结登记前已经精确读取的 EvidenceRef 和已经存在的 ExecutionRef，因此结果产生后的登记不会被表述为预注册。登记不会因为策略存在缺项而失败；缺项会作为策略决定保存，并在真正声明依赖该策略的行动准入时生效。

## 来源关系

关系类型固定为 `USED`、`GENERATED`、`DERIVED_FROM`、`CITED`、`SUPPORTED_BY`、`CHECKED_BY`、`INVALIDATED_BY`。每条边明确区分：

- `DECLARED_PROVENANCE`：协议计划使用的输入、方法或转换依据。
- `OBSERVED_PROVENANCE`：平台确实读取、观察或用于主张的精确对象。

AssetRef 必须由 Data 返回完全相同的引用；EvidenceRef 必须经过 Knowledge 精确读取并产生读取回执；ExecutionRef 必须属于当前项目和任务。SearchHit、URL、自由文本和跨项目引用不能替代正式来源。重复关系、冲突修订和不精确引用会被拒绝或按确定性关系摘要去重。

## 主张

主张类型为 `DESCRIPTIVE`、`ASSOCIATIONAL`、`PREDICTIVE` 或 `CAUSAL`，并保存对象、时空范围、可选数量和单位、限制以及实际来源。支持状态为 `PROPOSED`、`SUPPORTED`、`UNVERIFIED`、`CONFLICTING` 或 `WITHDRAWN`。`SUPPORTED` 至少需要一个精确的 `SUPPORTED_BY` 来源；它仍不等同于 P5.3 的独立科学验收。因果主张必须明确限制。

主张只能绑定当前、未被替代的分析登记。修订需要原 Claim ID、预期修订号和原因；历史版本不改写。

## 地下水比较策略

内置策略 `groundwater-head-comparison-v1@1.0.0` 是标准库纯函数，只读取已经取得的结构化事实。结果为：

- `NOT_APPLICABLE`：协议没有声明跨井、跨点或跨来源水头比较。
- `MISSING`：缺少监测点身份、量的类型、单位、垂向基准、时间范围、聚合口径或必要转换来源。
- `CONFLICT`：单位不可换算、垂向基准无转换、时间范围或聚合规则冲突。
- `APPLICABLE`：当前结构化前提足以开始该比较。

旧执行请求不需要研究上下文。新执行可以选择携带：

```json
{
  "research_context": {
    "analysis_registration_id": "registration-id",
    "policy_requirement": "groundwater-head-comparison-v1"
  }
}
```

只绑定登记而不声明 `policy_requirement` 时，行动用于协议追踪，不会被局部比较策略阻止。声明策略依赖时，准入事务要求冻结决定为 `APPLICABLE`。策略快照随 Action 保存；新协议或新策略版本不会改写旧执行。当前公开算子没有新增水头比较算法，本接口只建立可信准入和来源基础。

P5.2 不提供上传策略、表达式、规则代码或模型生成判定器的入口，也不把自然语言差异自动判为结构化冲突。
