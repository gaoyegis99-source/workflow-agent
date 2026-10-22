# P7.3 Join / Aggregate 复评与后续顺序

## 决策

P7.2a–c 已经形成一条可验证的单输入表格链：Project 负责列身份，Filter 负责行选择，Cast 负责显式类型变化。复评结论如下。

| 候选能力 | 当前决定 | 原因 | 后续位置 |
| --- | --- | --- | --- |
| `table-aggregate@1.0.0` | **有条件进入下一独立轮次** | 仍是单输入算子，能够复用精确 `TableSelection`、`TABULAR_V1`、原子发布、独立复算和 Agent 冻结意图 | P7.3a 候选；先冻结下述窄协议，再实施和测试 |
| `table-join@1.0.0` | **当前不实施** | V7 的公开 DTO、Platform 准入、Execution 暂存、Agent 意图和 checker 均只有一个 `table` 输入；直接加入 Join 会产生跨层的隐式多输入语义 | 先实施 P7.3b 多输入基础，再单独决定受限 Join 试点 |

这不是以功能多少作判断。Aggregate 的风险可以在一个算子版本内用有限操作、明确 null 规则、确定顺序和独立复算封闭。Join 会改变平台对输入身份和来源的基本表达，而且最坏输出会从两个 100,000 行输入放大到 100 亿行；未先补齐多输入身份和上限仲裁时，不应把它包装成普通 `TABULAR_V1` 算子。

本轮只完成复评和设计冻结，不新增算子、数据库迁移、公开路由或发行版本。

## 当前实现证据

当前代码中的单输入假设是显式的，并非文档上的限制：

- Platform 在写入 Action、预算和 outbox 前要求目录恰好只有一个输入端口，再只调用一次 Data `/resolve`：`packages/platform-core/src/platform_core/application/science.py:125-135`。
- 公开 `TableExecutionInputs` 只有 `table: TableSelection`，表格请求联合只包含五个现有算子：`packages/contracts/src/platform_contracts/science.py:639-669`。
- `AgentOperatorIntent` 只有一个 `input: TableSelection`：`packages/contracts/src/platform_contracts/control.py:38-64`。
- Execution 将 `TABULAR_V1` 约束为 `{"table"}`，并固定暂存到 `table.parquet`：`apps/execution-service/src/platform_execution/application/runner.py:478-556`。
- 表格 checker 只取得一个 `source_table`，来源 manifest 也只读取 `inputs.table`：`packages/checker-pack/src/platform_checkers/tabular.py:899-955`。
- Data 将表格资产限制为 100,000 行、128 列和四种平面标量：`apps/data-service/src/platform_data/domain/model.py:6-8`、`apps/data-service/src/platform_data/application/data.py:218-233`。

所以 Aggregate 只需在现有联合中增加一个严格参数契约和对应实现。Join 至少需要同时改造五条边界；若只在容器中私自读取第二个文件，Platform 无法授权第二个输入，provenance 无法证明左右角色，checker 也无法独立复核。

## 成熟项目提供的约束经验

成熟项目没有把 Join 或 Aggregate 视为一个无条件的通用函数，而是把危险语义放到显式参数中：

- [Polars DataFrame.join](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join.html) 提供 `validate` 限制一对一、一对多和多对一，单独声明 null 是否相等，并把输出顺序作为显式选项。这支持 V7 冻结基数、null 和顺序，而不是采用后端默认值。
- [pandas.merge](https://pandas.pydata.org/docs/reference/api/pandas.merge.html) 同样提供基数校验和来源指示；其文档特别说明 null 键之间会相互匹配，与通常 SQL 行为不同。这证明 null 键语义必须写入算子版本，不能依赖用户对某个库的经验。
- [Apache Arrow Table](https://arrow.apache.org/docs/python/generated/pyarrow.Table.html) 已提供 join 和 group-by 原语，可以作为固定实现的执行组件；但它不替代授权、输出放大限制、单位含义、provenance 或独立验收。
- [DuckDB Aggregate Functions](https://duckdb.org/docs/current/sql/functions/aggregates) 说明浮点聚合以及 first、last、list、字符串拼接等操作会受输入顺序影响，多数聚合忽略 null，而空组通常产生 null。这支持首版排除顺序敏感集合聚合，并自行冻结空输入和 null 规则。

V7 可以借鉴这些契约，不需要把 Polars、pandas 或 DuckDB 变成新的平台层。现有 PyArrow 固定容器足以承载首版 Aggregate；实现库只是可替换的内部细节，公开语义由 V7 契约决定。

## V6 可复用的经验与不能沿用的部分

V6 `table_join.py` 已具备几项正确保护：

- 显式键对及一对一、一对多、多对一、多对多声明；
- null 键不匹配；
- 物化前计算结果行数并检查 `maxOutputRows`；
- 按左右源表序号产生稳定顺序。

这些规则应保留为未来 Join 的设计输入。但 V6 自动添加 `left__` / `right__` 前缀、允许多对多、把规则放在第三个资产中，也没有接入 V7 精确左右输入身份、工作流来源、Agent 意图和独立 checker。因此不能直接迁入。

V6 的 `timeseries.aggregate` 是按日历周期处理时间序列的领域算子，包含 median 和标准差。它没有完整冻结 null、非有限值、空组、浮点顺序或单位传播，不能作为通用表格 Aggregate 的契约。日历分桶、时区和地下水统计口径以后仍应属于领域包，而不是塞进 `table-aggregate@1.0.0`。

## P7.3a 候选：受限 `table-aggregate@1.0.0`

### 输入和参数

首版继续使用一个精确 `TableSelection` 和现有 `TABULAR_V1`。参数建议冻结为：

```json
{
  "semantics": "strict-table-aggregate-v1",
  "group_by": ["f0"],
  "null_key_policy": "GROUP",
  "measures": [
    {
      "measure_id": "mean_level",
      "operation": "MEAN",
      "field_id": "f2",
      "display_name": "平均值"
    }
  ]
}
```

约束：

- `group_by` 为 0–16 个不同字段；v1 分组键只接受 string、int64、bool，暂不接受 double，避免冻结 NaN、`-0.0` 及不同后端哈希语义。
- 1–64 个不同 `measure_id`。操作只允许 `COUNT_ROWS`、`COUNT_NON_NULL`、`COUNT_DISTINCT`、`MIN`、`MAX`、`SUM`、`MEAN`。
- `COUNT_ROWS` 不引用字段；其他操作必须引用一个字段。`MIN/MAX` 接受四种现有标量，`SUM/MEAN` 只接受 int64/double。
- 不提供任意表达式、HAVING、窗口函数、median、标准差、first/last、list 或字符串拼接。
- `null_key_policy` 固定三选一：`GROUP` 将组合中的 null 作为一个明确组，`DROP` 丢弃含 null 键的行并报告数量，`ERROR` 在发现后明确失败。

### 数值、null 和空输入

- null measure 不参加 `COUNT_DISTINCT/MIN/MAX/SUM/MEAN`，但其数量写入 report。
- NaN 和正负无穷不能参加数值聚合；发现即以 `NONFINITE_VALUE` 失败，不静默忽略。
- int64 `SUM` 使用带溢出检查的 int64 结果；溢出以 `NUMERIC_OVERFLOW` 失败。
- double `SUM/MEAN` 使用固定源行顺序和确定性求和算法。建议生产实现固定为 `math.fsum` 分组累积，独立 checker 用 Decimal 高精度参考并按冻结容差比较，不能依赖多线程后端的未声明合并顺序。
- 无分组键的空表固定输出一行：count 为 0，其他操作为 null。存在分组键的空表输出零行。
- 组按其在源表第一次出现的顺序输出；measure 按请求顺序输出。禁止依赖哈希表、locale 或执行引擎的隐式顺序。

### 字段、语义和成果

输出字段重新分配稳定 `f0…`，manifest 为每个输出字段保存 `source_kind=GROUP_KEY|MEASURE`、源字段、操作及参数摘要。成功成果仍是 `report/table/method`：

- `report` 记录输入行、参与行、丢弃行、组数、每项 measure 的非空/null 数、非有限值和溢出事实；
- `table` 使用 `table-aggregate-table-v1`；
- `method` 冻结算法、顺序、null、空输入和数值规则；
- `report` 端口使用 `table-aggregate-report-v1`，由 `tabular-aggregate-reproduction-v1` 独立复算。

语义传播保持保守：COUNT 无量纲；MIN/MAX/MEAN 可以保留输入单位声明；SUM 只保留维度单位，不证明“可相加”、时间加权或业务充分性。没有受信语义修订时，输出语义标记为 `UNDECLARED`。通过 `DATA_VALIDITY` 和 `RESULT_REPRODUCTION` 只证明计算可复核，不自动产生 `CLAIM_SUPPORT`，也不证明真实观测或地下水解释正确。

### P7.3a 门槛

实现轮次必须覆盖：

1. 所有操作的空表、全 null、重复值、Unicode、边界整数、溢出和非有限数值。
2. 三种 null key 策略、复合键、稳定组顺序和同输入确定性摘要。
3. 生产算子与 checker 代码隔离，错误输出不能通过独立复算。
4. 直接执行、Agent VERIFIED、`Filter → Aggregate → Profile` 工作流、失败不发布及响应丢失恢复。
5. 一次直接 VERIFIED 闭环仍按计算、runtime integrity、领域复算消耗三次额度。
6. P0–P7.2c 完整兼容回归通过后才结束该轮。

## P7.3b 前置：多输入表格基础

Join 进入实现前必须先完成以下平台能力，而且每一项都要有失败用例：

1. **公开契约**：增加判别明确的多输入表格请求，左右各自保存完整 `TableSelection`；不把旧单输入 DTO 改成任意字典。
2. **Platform 准入**：按冻结 Operator Catalog 精确比较全部输入端口，分别进行 Data `/resolve`、项目权限和 grant 核对，并在同一 Action 中冻结两个摘要。
3. **Execution 暂存**：按受信端口名分别保存只读输入，例如 `tables/left.parquet` 和 `tables/right.parquet`；每个文件独立检查摘要和 Schema。
4. **来源与发布**：manifest 同时保留 `left`、`right` 的 AssetRef、表示摘要、角色和工作流来源，任何一侧不可被新修订替换。
5. **工作流绑定**：两个输入可以分别来自根资产或不同上游节点，且完整调用路径、输出端口和数组位置保持精确。
6. **Agent 意图**：TaskSpec 冻结两个输入和完整参数；模型不能交换左右角色、键、基数或上限。
7. **独立 checker**：输入包取得两张精确源表并复核 pair mapping、输出 Schema、行身份及 report；通用 runtime checker 不能兜底产生科学 PASS。
8. **放大仲裁**：执行前冻结强制输出行数和字节上限，运行时先计数再物化；未知状态保持原 Action 和预算，不换身份重算。

基础层必须先用一个测试专用双输入夹具证明授权、工作流、恢复和 provenance，不同时加入生产 Join，以便故障能够归因。

## 未来 Join 试点的最小安全范围

多输入基础通过后，首版 Join 仍应限制为：

- 只支持 `INNER` 和 `LEFT` 等值连接；1–16 对显式键。
- 键的物理类型必须逐对完全相同，只接受 string、int64、bool；不自动 cast，不支持 double、时间、区间、模糊或 as-of 连接。
- `nulls_equal=false` 固定；含 null 的键不相互匹配。
- 调用者必须声明 `ONE_TO_ONE`、`ONE_TO_MANY` 或 `MANY_TO_ONE`；v1 拒绝 `MANY_TO_MANY`。
- `max_output_rows` 必填且不超过 100,000；同时受 100 MiB 成果上限约束。物化前超限则失败，不截断。
- 输出按左表原始顺序、同一左行内按右表原始顺序；未匹配 LEFT 行保持左顺序。
- 输出字段必须以显式投影和目标显示名声明；冲突时拒绝，不自动加后缀猜测意图。
- report 至少记录左右行数、null key 行、各侧重复 key、匹配 pair、未匹配行、输出行和放大比率。
- `tabular-join-reproduction-v1` 从两张精确源表独立复算；执行失败不发布资产，已开始则按现有规则计费。

该范围故意排除 full/right/many-to-many、隐式类型转换和任意连接表达式。确有业务需求时应以新版本或领域算子评估，不能扩展 v1 的既有语义。

## 推荐实施顺序

1. **下一轮选择 P7.3a Aggregate**：它能在现有单输入架构上形成完整、独立可验收的闭环，改动面最小且通用价值明确。
2. **P7.3a 通过后重新确认 Join 的真实用例**：至少要有两个明确数据角色、键关系和期望基数；没有真实用例就不建设多输入基础。
3. **存在需求时实施 P7.3b 多输入基础**：本轮只证明身份、授权、恢复和来源，不实现生产 Join。
4. **再开 P7.3c 受限 Join 试点**：严格遵守上述范围；若多输入基础或输出放大测试未通过，继续阻断。

因此当前最合理的下一步是为 `table-aggregate@1.0.0` 单独制定实施计划并进入一轮开发测试。Join 保持延期，直到多输入基础和一个真实一对一/一对多用例同时具备。
