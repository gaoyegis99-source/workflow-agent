# P6.4 / 0.8.4 受控单算子 Agent 闭环报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-15T03:07:50.026733+00:00

## 完成内容

- 冻结 P6.4 工具目录在原 15 个只读工具上新增三个受控写工具：`execution.submit_operator`、`deliverable.register`、`acceptance.request`。
- 算子提交只允许 P3 的完整率与描述性线性趋势。服务端从当前 TaskSpec 取得精确输入、参数和验收范围，从受信目录取得实现，模型不能提交镜像、命令、地址或任意参数。
- Agent continuation 不保存用户 Bearer Token；每次工具准入、outbox 发送和 Execution permit 都按发起主体重新检查当前项目角色、任务修订、grant 与预算。
- Platform `0014_agent_action_links` 保存 ToolRequest 到 Action、Command、Deliverable 或 Acceptance 的稳定关系。同一请求重放不会重复计算、扣账或登记成果。
- Action 状态投影器把已提交、运行、发布与验收事实转换为有版本的 Agent 事件。Acceptance PASS 后允许最后一个只读终态帧，模型只能提交 `EXPLANATION`。
- 真实 P3 容器闭环已经打通：两个算子各执行一次，整组成果登记一次，独立 Decimal 检查一次，预算最终为 used=3、reserved=0、in_flight=0，Task 转为 COMPLETED。

## 测试结果

- P6.4 专项：9 passed，0 failed，0 skipped。
- P6.4 至 P6.0、P2、契约和架构聚焦回归：315 passed，0 failed，0 skipped，用时 125.622 秒。
- HTTP/OpenAPI 定向回归：11 passed，0 failed，0 skipped。
- 十个 wheel 和十个 sdist 构建成功；仓库外安装、Action 工具目录、运行时、checkpoint 与生产 Worker 组合入口通过。

## 范围与结论

P6.4 已形成“模型提出受控计算 → 既有 P3 Action/预算/授权 → 真实容器与发布 → 交付物 → 独立验收 → Task 完成 → 最终解释”的第一条自主业务闭环。科学结果和完成事实仍由 Execution、Data 与 Acceptance 掌握，模型只能提出有类型的请求。本轮按要求聚焦逻辑，没有重复 P4 长性能与远程组合矩阵。下一轮 P6.5 负责更通用的长等待、依赖水位、服务事件和进程恢复。
