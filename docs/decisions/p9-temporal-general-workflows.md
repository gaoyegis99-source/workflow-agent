# P9：通用 Agent 工作流采用 Temporal，但不转移 V7 治理权

日期：2026-10-24
状态：已采纳（首期单机恢复目标）

## 决定

P6 的结论保持有效：LangGraph 仅用于受控 Agent 决策建议，账本、权限、预算、Action、执行结果和恢复事实仍由 V7 拥有。P9 新增的是一个不同范围的执行后端：已发布的通用工作流使用 Temporal 推进定时器、等待、Activity 重试、子流程和编排重放。

责任边界固定如下：

| 层 | 唯一职责 |
| --- | --- |
| Temporal | 计划/推进步骤、等待审批信号、定时器、Activity 重试、子工作流、历史重放 |
| V7 治理内核/账本 | Action 身份、审批事实、权限与预算复查、发布版本冻结、业务结果认定、outbox |
| Agent 运行时 | 从冻结上下文生成 `DecisionProposal`；现有 LangGraph 适配器仍留在此层 |
| 执行插件宿主 | HTTP、MCP、容器/CWL/Toil、远程计算和 Agent 调用的真实 I/O 与结果查询 |

`packages/temporal-workflow-runtime` 的 Workflow 代码不能直接调用模型、HTTP、数据库、MCP 或容器。它只调度 `v7_execute_governed_node` 和事件 Activity；Activity 先经 V7 账本授权，才调用已审核插件宿主。

该包还提供 `create_governance_router`，实现 Worker 到 Platform 的私有
`/internal/temporal/v1` HTTP 线协议。Platform 组合层把现有事务性账本适配为
`GovernanceLedger` 后再挂载该路由；Worker 只持有挂载的服务令牌和
`HttpGovernanceLedger`，没有数据库连接。该路由不新建第二套业务状态。
`SQLiteGovernanceLedger` 仅用于本地开发和重启契约测试，不能作为生产
Platform 的账本替代品。

## 幂等与恢复

Temporal Activity 可重复执行，因此不把它作为外部副作用“恰好一次”的证明。每个节点从 `(run_id, node_key, workflow definition digest, exact plugin reference, frozen input, release digest)` 生成稳定的 `action_id` 和 `idempotency_key`。账本先持久化准入/预算预留，再允许 Activity 调用插件。

如果调用后回执丢失，账本记录 `UNKNOWN`，Activity 返回 `RECONCILING`，Workflow 等待一个明确的 `reconcile` 信号。它不会用另一个身份重发原调用。`dispatch` 是原子准入：只有得到 `invoke=true` 的 delivery 可以首次调用插件；并发或迟到 delivery 只按原幂等键查询外部系统。插件宿主应使用原幂等键查询外部系统；确认结果后才结算原预留。取消同样是显式信号和账本事件，不能假定外部操作已经停止或能够自动补偿。

执行前再次验证权限、预算、发布版本及工具清单。框架重放不能让已经撤销的授权复活。

## 版本、回退和兼容

- 新通用流程固定 `v7-general-workflow-v1`、定义摘要、发布摘要和精确插件包摘要。
- 新运行使用 `temporal-v1` 后端；旧运行继续由其原 Worker/运行时收尾。禁止在途迁移检查点或共享可写状态。
- 回退时停止向 Temporal 创建新运行，但让对应版本 Worker 继续处理已创建运行，直至关闭。
- `import_v6_bundle` 仅转换可验证等价的 V6 工具、审批、条件和并行步骤；它逐项复核精确工具摘要、权限和凭据绑定，并把 V6 文件列为待绑定的受控输入。V6 Agent SDK 步骤、子流程、journal、SDK checkpoint、callable 和工具包代码都不导入；应先在 V7 发布对应 Agent 插件或子流程后再创建新运行。

## 首期边界

首期节点为 Agent、HTTP、MCP、人工审批、定时等待、容器计算、条件、并行、有界循环和子流程。Webhook/API/手动触发先由 Platform 事务写入 `TemporalDispatchCommand` 和既有 outbox，再由 `TemporalOutboxDispatcher` 调用 `TemporalTriggerClient`；定时触发使用 Temporal Schedule 和 `V7ScheduledInvocationWorkflowV1` 为每次触发生成新的业务 run identity。多 Agent 通过明确子流程和各自冻结的插件/预算范围组合，不允许无限制自主派生。
Agent 节点的 `max_turns` 是冻结 Action 命令的一部分，Activity 只会把它交给
Agent 插件；任何非 Agent 插件携带该字段或 Agent 插件缺少该字段均会被拒绝。

不在此阶段建设知识库、业务数据库或数据目录；已有专业 CWL/Toil 链路保留为容器计算插件后端。

## 运维目标

首期 Compose profile 运行独立 Temporal Service、其 PostgreSQL 和 V7 Worker，目标是进程/Worker 重启恢复，不承诺多机高可用或整机失效无中断。生产发布需要实际故障演练和调度延迟/恢复耗时基线；Temporal 开发服务器仅用于开发测试。
