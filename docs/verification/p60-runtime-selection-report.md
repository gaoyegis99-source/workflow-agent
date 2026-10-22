# P6.0 / 0.8.0 Agent 运行时选型报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-14T23:53:23.823889+00:00

## 完成内容

- 新增只依赖标准库的 `DecisionFrame`、`DecisionProposal`、RuntimePort、ModelPort 和 ToolTransport 边界；运行时只能返回提议，不能直接执行业务工具。
- 新增 `plain-loop-v1` 语义基线和 `langgraph-minimal-v1` 最小适配器；两者保持相同的单次决策、重放、唤醒、中断和可见轨迹语义。
- 选定 LangGraph `1.2.15` 与 PostgreSQL checkpointer `3.2.0`。真实 PostgreSQL 验证了适配器对象重建后复用原 checkpoint 和原 proposal。
- profile、框架版本、checkpoint 类型与控制模式均使用规范摘要冻结；客户端不能选择浮动工具版本或令运行时自动调用工具。
- 新增第十个发行包 `groundwater-agent-runtime`，十个包统一为 `0.8.0`，并在仓库外以锁定依赖完成安装和运行探针。

## 测试结果

- P6.0 专项：27 passed，0 failed，0 skipped。
- 契约、架构与 P6.0 聚焦回归：168 passed，0 failed，0 skipped，用时 4.342 秒。
- 完整回归：680 passed，0 failed，0 skipped，用时 1698.377 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、依赖检查、profile probe 和两个适配器重放语义通过。

## 问题处理

- LangGraph 1.2 会把非空 `checkpoint_ns` 解释为子图路径。顶层运行时改用固定前缀、profile 摘要和 AgentRun ID 组成 `thread_id`，避免恢复时出现 `Subgraph ... not found`。
- `ToolRequest` 增加独立版本校验，拒绝 `latest`，避免绕过已有版本化契约的固定版本要求。

## 范围与结论

P6.0 只冻结运行时边界和适配器选择，不开放公开 AGENT Task，不调用真实模型，不执行 Data、Knowledge 或 Execution 工具，也不新增 Platform 业务迁移。P4 调度代码未变化，因此未重复长性能矩阵。

P6.0 通过并结束。本阶段形成的 RuntimePort 是 P6.1 确定性 Agent ledger 的基础；P6.1 尚未实施。
