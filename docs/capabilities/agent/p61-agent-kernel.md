# P6.1 确定性 Agent 内核

版本：`0.8.1`
Platform schema：`0011_agent_coordination`
Agent checkpoint schema：`0001_langgraph_checkpoint`

## 权威边界

Platform ledger 是任务协调的唯一权威来源。它保存 AgentRun、TaskSpec 修订、计划修订、ContextManifest、DecisionFrame、Proposal、问题与回答、事件水位、wakeup、outbox 和租约。LangGraph checkpoint 只保存运行时恢复材料，不能增加任务权限、改写计划、执行工具或证明业务动作已经发生。

每次决策遵循固定顺序：

```text
领取 AgentRun 租约
→ 核对 TaskSpec revision、generation 和 runtime epoch
→ 对比 checkpoint 与 ledger revision
→ 冻结 ContextManifest 和 DecisionFrame
→ 运行时生成一个结构化 Proposal
→ 在同一 Platform 事务中接受 Proposal 并推进 ledger
→ 释放租约
```

一个 frame 最多接受一个 proposal。相同 frame 和 proposal 摘要可以安全重放；内容冲突返回 `INVALID_BINDING`。旧 generation、旧 epoch、旧 TaskSpec revision 和已失效 frame 均不能写入计划或等待状态。

## 任务和事件语义

- Task 修订提升 Agent generation，使进行中的 frame 变为 `STALE`，并创建新 wakeup。
- 暂停阻止继续决策；恢复在当前 TaskSpec 上创建新 wakeup；取消使 AgentRun 停止。
- 问题回答按 Idempotency-Key 去重，固定回答所针对的 TaskSpec revision。
- 事件按来源、stream 和 revision 排序。同一事件 ID 相同内容为重放，不同内容为冲突；旧 revision 不覆盖新事实。
- checkpoint 缺失、损坏、落后或超前时都不直接信任运行时内容。Worker 从 ledger 建立新 epoch 和 runtime identity。
- checkpoint 暂时不可用只重新排队当前 AgentRun，不提升 epoch，也不伪造恢复成功。

## 功能开关边界

默认 `create_app(database)` 和 `ControlService(database)` 继续拒绝 `execution_mode=AGENT`。P6.1 测试部署必须显式注入冻结的 runtime profile 才能创建 AGENT Task。该注入不是公开请求字段，客户端不能选择 profile、checkpoint 或实现。

P6.1 只使用 scripted decision fixture。没有真实模型调用，没有模型预算，没有 Data/Knowledge 工具，也不会创建 Execution Action。

## 安装和迁移

升级时停止 Platform API 与 Agent Worker，安装十个 `0.8.1` 包，然后分别迁移两个数据库：

```bash
uv sync --locked
uv run platform-admin migrate
export V7_AGENT_CHECKPOINT_DATABASE_URL='postgresql://.../v7_agent_checkpoint'
uv run agent-checkpoint-admin migrate
uv run platform-admin check-schema
uv run agent-checkpoint-admin check-schema
```

两个数据库必须使用不同数据库和凭据。应用启动只检查版本，不自动迁移。回退到 `0.8.0` 前必须让测试 AGENT Task 停止；业务数据库按备份恢复，不能通过删除 ledger 伪造回退。

## 验收

```bash
bash scripts/checks/check_p6.sh --phase kernel
bash scripts/checks/check_p6.sh --phase all
```

`kernel` 运行 P6.1、P6.0、P2、契约和架构聚焦回归并构建发行包。`all` 额外执行一次完整回归、仓库外安装验证和结构化证据生成。P4 长节点性能矩阵不在本阶段重复执行，因为调度器、Toil 和 Execution 没有变化。
