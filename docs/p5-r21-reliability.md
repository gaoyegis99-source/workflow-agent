# P5-R2.1 / 0.7.2 真实组合检查可靠性

P5-R2.1 不增加新的科学能力、公开接口或数据库字段。它补齐 P5-R2 中缺少的真实跨服务证据，证明精确 Deliverable 和 AnalysisRegistration 能够经过 Platform Worker、Execution Service 以及两个受信 checker 容器完成 VERIFIED 任务。

## 已验证链路

```text
精确成果角色绑定
  → CoveragePlan v3
  → runtime-integrity Action
  → Execution 容器
  → P3 Decimal reproduction Action
  → Execution 容器
  → 预算结算
  → Acceptance PASS
  → Task COMPLETED
```

测试使用真实 PostgreSQL、独立 Platform/Data/Execution 进程和固定 Docker 镜像。正常链路恰好启动两个 checker Action，连同前置趋势算子共消耗三次执行额度。

故障链路覆盖：

- API、Platform Worker 和 Execution Worker 重启后，以原幂等键恢复同一 Acceptance，并保留原 Action 和 Execution 身份。
- TaskSpec 在 checker 启动前修订时，CoveragePlan 变为 `STALE`；未启动检查不执行，剩余 envelope 被释放。
- 测试专用延迟镜像提供确定性终止窗口。真实 checker 容器被强制终止后，首次 Acceptance 为 `ERROR`。
- 显式 `retry_of` 只重新创建失败能力及其未运行下游，不复用新身份掩盖失败，也不重复结算。
- Task 取消会停止已启动 checker，阻止后续能力启动，并释放未使用额度。

延迟镜像只在 `tests/p5r21` 构建和使用，不进入产品目录、能力目录或发行镜像。

## 运行

聚焦可靠性门禁：

```bash
bash scripts/check_p5_r21.sh
```

完整发行门禁：

```bash
bash scripts/check_p5.sh --phase all
```

完整门禁还验证九个发行包、两个固定 checker 镜像、能力目录、仓库外安装、HTTP 响应丢失、P1 规则以及 P0–P5 回归。

## 升级与回退

0.7.2 沿用以下 schema：Platform `0010_checker_runs`、Data `0004_document_assets`、Execution `0006_remote_reliability`、Knowledge `0002_lifecycle_outbox`。升级不执行数据库迁移，但 API、Worker、checker 镜像和 capability catalog 必须来自同一源码摘要，旧新进程不得混跑。

回退到 0.7.1 不需要数据库降级。应先让活动 checker Action 收敛，再整体切换九个包、固定镜像和目录锁；不得删除已有 Acceptance、Action、预算或报告。

## 范围

本轮只关闭组合运行可靠性缺口。未知领域仍为 `UNCOVERED / INDETERMINATE`，关联、预测和因果主张仍不能获得自动 PASS。P6 Agent 未开始实施。
