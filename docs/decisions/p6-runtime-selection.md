# P6.0 Agent 运行时选型记录

日期：2026-10-15  
状态：已选择并通过 P6.0 完整门禁  
版本：`0.8.0`

## 决定

P6 主运行时选择 `langgraph-minimal-v1`，保留 `plain-loop-v1` 作为框架无关的语义基线。

LangGraph 只保存 Agent 推进位置和可见运行轨迹。Task、计划准入、Action、预算、Execution、Data、Knowledge、Acceptance 和 Completion 仍由 V7 业务账本拥有。运行时只能返回 `DecisionProposal`，不能调用业务工具或写入业务状态。

固定依赖：

- `langgraph==1.2.15`
- `langgraph-checkpoint-postgres==3.2.0`
- Python `3.12`

生产 profile 使用 PostgreSQL checkpoint。测试允许注入 `InMemorySaver`，但不会形成可发布的运行配置。

## 实测结论

两个适配器使用相同 `DecisionFrame`、`DecisionProposal` 和 `RuntimePort`：

- 同一个 frame 重放返回原 proposal，不再次调用 decision function；
- 同一 frame ID 对应不同内容时拒绝；
- 每次 advance 最多产生一个 decision；
- wake batch 作为观察进入下一 frame；
- interrupt 幂等，停止后不能继续决策；
- runtime 只产生行动建议，不执行建议中的业务工具；
- PostgreSQL checkpoint 可在 adapter 进程对象重建后恢复原 proposal；
- checkpoint 连接失效返回 `RUNTIME_UNAVAILABLE`，不会调用 decision function；
- profile、框架版本、checkpoint 类型和控制模式使用摘要锁定。

## 已发现并修复的问题

LangGraph 1.2 把非空 `checkpoint_ns` 解释成已编译子图路径。顶层图若把它当普通隔离标签使用，`get_state` 会报 `Subgraph ... not found`。V7 改用“固定前缀＋runtime profile 摘要＋AgentRun ID”组成 `thread_id`，隔离顶层图且不占用子图命名空间。

现有 `VersionId` 的 `latest` 拒绝逻辑位于 `VersionedContract`。P6 ToolRequest 直接使用该字符串类型时不会继承模型校验。ToolRequest 因此增加独立固定版本校验，禁止模型使用浮动工具版本。

## 未采用方案

- Deep Agents：内置计划、文件系统、memory 和子 Agent 与 V7 权威重叠。
- OpenAI Agents SDK 自动工具循环：会模糊单一副作用执行路径。
- Microsoft Agent Framework/AutoGen：首期单 Agent 不需要第二套 workflow/message runtime。
- Temporal：现有 PostgreSQL outbox 和业务账本足够，增加它会形成额外恢复权威。

## 回退

若后续 LangGraph 升级无法通过身份、恢复或副作用门禁，选中 profile 改为 `plain-loop-v1`。业务对象不需要迁移；尚未终态的 LangGraph checkpoint 保留只读，AgentRun 由业务账本以新 runtime epoch 恢复。

P6.0 不开放公开 AGENT Task，不调用真实模型，也不创建 Platform 业务迁移。
