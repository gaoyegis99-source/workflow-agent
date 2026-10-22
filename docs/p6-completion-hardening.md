# P6 / 0.9.2 完成度补强

本次补强关闭了 0.9.0 条件发行中的本地闭环、组合恢复、发行安装和真实模型门槛。

## 科学闭环

Agent 继续通过冻结工具目录调用既有应用用例，不获得用户 Token、镜像、命令或数据库写权限。新增的 `deliverable.register_exact` 根据 Platform Action、Execution 发布回执和 Data manifest 反查并规范化 `BoundOutputRef`；调用者不能自行声称节点、端口、资产或摘要。`acceptance.request` 可以携带正式分析登记，因而 Agent 能完成精确角色绑定后的 `open-science-v1` 验收。

真实门禁包含：

- 两个 P3 算子组成的 Toil 工作流；
- QGIS Buffer、Native Summary 和 QGIS Static Map 三节点工作流；
- 趋势成果的 runtime integrity 与 Decimal 独立复算；
- VERIFIED Completion、唯一预算结算和原子发布。

内部 Agent 验收没有用户 Token。迁移 `0018_agent_acceptance_identity` 只把 `acceptance_check_envelopes.token_id` 改为可空；Action 的 Agent 身份、项目、任务和冻结计划仍是权威审计边界。

## 恢复与运维

`agent-admin rebuild-checkpoint --agent-run-id ...` 从 Platform 权威账本创建新 runtime epoch。存在活动租约或正在处理的 frame 时拒绝；WAITING/QUESTION 等控制状态保持原状，旧 generation 不能继续写入。

`agent-ledger-backup` 分别以 PostgreSQL repeatable-read 快照导出 Platform 和 LangGraph checkpoint 存储。manifest 记录 schema、表、行数和 SHA-256。恢复前再次验证 manifest、文件摘要和目标 schema，并且要求 `--confirm-restore`。恢复用于受控停机窗口，不能与活动 API/Worker 混跑。

设置 `V7_AGENT_OTEL_ENDPOINT` 后，Agent Worker 使用 OTLP/HTTP 输出 trace。属性白名单仅包含 Agent Run、Task、epoch、generation、proposal kind 和 proposal ID；不输出 Token、prompt、工具载荷、正文或凭据。没有 endpoint 时不启动 exporter，也不影响 readiness。

## 评测边界

A01–A15 冻结到精确 pytest node ID，并由三轮 JUnit 证明实际执行，最小稳定性为 100%。真实 Provider 驱动读取[冻结目录](../deploy/p6/model-catalog.deepseek-flash.json)，对 15 个场景各执行三次，检查身份、Proposal、工具和关键参数并统计耗时与费用。`deepseek-flash-2026-10-15` 的 45 次正式运行全部正确，严重错误、质量门槛失败和 usage 缺失均为 0，总费用为 834,622 微单位；没有用 fake provider 或 record/replay 替代真实结果。
