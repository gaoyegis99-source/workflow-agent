# P6.3 / 0.8.3 上下文与只读 Agent 报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-15T02:38:51.512851+00:00

## 完成内容

- `ContextCompiler v1` 按固定政策、Task 控制事实、必要要求、只读目录和观察组装 ContextManifest v2；每部分记录摘要与 token 估算，省略整个可选对象并留下原因，必要要求放不下时明确返回 `CONTEXT_OVERFLOW`。
- 部署冻结 15 个只读 ToolSpec。模型只能引用固定 ID、版本和参数结构，不能注册工具、提交镜像、命令、URL 或任意代码。
- Platform `0013_agent_read_tools` 在服务读取前保存稳定 ToolRequest，服务读取后只保存一条不可变 ObservationReceipt。重放使用原请求和观察，不生成业务 Action。
- Data 资产列表按当前 Task 精确 grant 过滤；资产、Knowledge、Execution、Research 和 Acceptance 查询均重新检查当前成员、项目和 Task 身份。Knowledge 精确读取使用稳定回执，SearchHit 仍不是 EvidenceRef。
- 同一 AgentRun 现在可以连续处理多个 DecisionFrame；runtime 身份按 AgentRun 校验，不再把第二个 frame 误判为新的 runtime。
- 提供生产 `agent-worker` 组合入口和 `agent-tool-catalog generate/check` 管理命令。

## 测试结果

- P6.3 专项：15 passed，0 failed，0 skipped。
- P6.3、P6.2、P6.1、P6.0、P2、契约和架构聚焦回归：306 passed，0 failed，0 skipped，用时 98.245 秒。
- HTTP/OpenAPI 定向回归：11 passed，0 failed，0 skipped。
- 十个 wheel 和十个 sdist 构建成功；仓库外安装、目录生成/校验、运行时 probe、checkpoint 管理和生产 Worker 入口通过。

## 范围与结论

P6.3 已打通“冻结上下文 → 模型提出读取 → 稳定 ToolRequest → 当前权限复核 → 精确服务读取 → ObservationReceipt → 下一帧解释”的闭环。资料正文与服务载荷不能提升为系统指令；WITHDRAWN 事实会原样呈现，不能被政策解释为当前支持证据。本轮没有开放任何科学计算或业务写入，因此任务不会因模型陈述而完成。按“不做过度兼容测试”的要求，本轮只运行受影响的 P6/P2/契约/架构门禁，没有重复 P4 长节点和远程故障矩阵。
