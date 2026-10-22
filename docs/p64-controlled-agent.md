# P6.4 / 0.8.4 受控单算子 Agent

P6.4 在 P6.3 的只读 Agent 上开放第一组受控写工具，并复用 P3 已有的 Task、grant、预算、Action、Execution、Data publication、Deliverable 和 Acceptance 用例。Agent 不拥有新的执行通道，也不能直接写入结果或完成事实。

## 冻结工具

| 工具 | 模型可提供 | 服务端决定 |
|---|---|---|
| `execution.submit_operator` | P3 算子 ID | 输入、参数、版本、实现、资源、grant、预算与 Action |
| `deliverable.register` | 已知 Execution ID 集合 | 发布事实、资产、来源、任务修订与候选摘要 |
| `acceptance.request` | Deliverable ID | P3 检查器、输入、预算、报告与 Task 完成 |

算子范围限定为 `timeseries-completeness@1.0.0` 和 `statistics-linear-trend@1.0.0`。输入和参数来自当前 `p3-timeseries-v1` TaskSpec；模型不能覆盖这些值。

## 身份与恢复

AgentRun 冻结发起主体，但 Worker 不保存用户 Bearer Token。每次写工具准入、Platform outbox 发送及 Execution permit 都用该主体重新检查当前项目 `EDITOR` 权限，并重新检查 Task revision、grant、预算、暂停与取消事实。

`agent_action_links` 把稳定 ToolRequest 关联到 Action、Command、Deliverable 或 Acceptance。同一 ToolRequest 使用同一幂等键；业务事务已经提交而工具响应丢失时，重放会恢复原业务身份。

Action 投影器只读取 Platform 已提交的业务事实，按单调 revision 产生 Agent 事件。它不查询或修改外部容器。Execution 和 checker Worker 仍负责外部对账。任务完成后只允许一个终态 `EXPLANATION` 决策，不能再提交工具。

## 部署

升级需要停止 Platform API、Platform Worker 与 Agent Worker，安装 `0.8.4`，显式迁移 Platform 到 `0014_agent_action_links`，生成 action 工具目录，再启动服务。`actions.token_id` 变为可空；`decision_source` 增加 `AGENT`。回退到 `0.8.3` 前必须保证不存在 P6.4 Action，因为旧版本不能解释无用户令牌的 Agent continuation。

统一门禁：

```bash
bash scripts/check_p6.sh --phase actions
bash scripts/check_p6.sh --phase all
```

P6.4 不开放空间、工作流、远程执行、研究修订、Claim、人工评审、grant、预算调整、任意 HTTP、脚本或代码执行工具。
