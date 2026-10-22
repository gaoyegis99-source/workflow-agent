# P6.1 / 0.8.1 确定性 Agent 内核报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-15T01:09:53.589013+00:00

## 完成内容

- Platform `0011_agent_coordination` 保存 AgentRun、不可变计划修订、ContextManifest、DecisionFrame、Proposal、问题/回答、事件、wakeup、outbox 和带代次租约。
- Agent Worker 每轮只领取一个租约、冻结一个 frame、取得一个结构化 proposal 并回写 ledger；运行时不能直接调用业务仓储或执行工具。
- Task 修订、暂停、恢复、取消和外部事件都会提升 generation、使旧 frame 失效并阻止旧 Worker 回写。
- LangGraph checkpoint 使用独立 PostgreSQL 数据库和显式 `0001_langgraph_checkpoint` 管理命令；缺失、损坏、落后或超前时从 Platform ledger 建立新 epoch，不接受 checkpoint 中未入账的 proposal。
- 默认 API 仍拒绝 AGENT Task；只有显式注入冻结 profile 的测试部署可以创建，用于本阶段内核验收。

## 测试结果

- P6.1 专项：18 passed，0 failed，0 skipped。
- P6.1、P6.0、P2、契约和架构聚焦回归：266 passed，0 failed，0 skipped，用时 82.305 秒。
- 完整回归：698 passed，0 failed，0 skipped，用时 1743.778 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、依赖检查、运行时 probe、checkpoint 管理 CLI 和两个 adapter 重放语义通过。

## 已关闭问题

- 回答事实中的数据库时间与 JSON 幂等回执分离，重复回答返回同一回执。
- 已接受 proposal 在响应丢失后可按同一 frame 和摘要重放，不会再次创建计划或副作用。
- 同一事件 ID 内容冲突、旧来源 revision、旧租约 generation 和旧 TaskSpec revision 均不能覆盖权威状态。

## 范围与结论

P6.1 已形成可恢复的确定性协调内核，但没有真实模型、模型费用、Data/Knowledge 工具或计算提交。生产入口继续拒绝 AGENT Task。P6.1 通过并结束；下一轮是 P6.2 模型网关和预算，本轮未实施。
