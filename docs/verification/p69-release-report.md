# P6 完成度补强 / 0.9.2 发行报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

A01–A15 已由精确 pytest node ID 各执行三次并通过。Agent 身份下的真实 Toil、QGIS 三节点工作流、P5 精确交付和开放验收均纳入门禁；备份恢复、受控 checkpoint 重建及可选脱敏 OpenTelemetry 已补齐。模型响应丢失、Agent Worker 接管、Execution Worker 与 Toil leader 终止、远端 peer 重启和 Data 发布响应丢失也已在同一条真实工作流中完成组合验证。

真实模型 A01–A15 各运行三次并通过冻结门槛：严重错误 0、usage 缺失 0，共 45 次模型调用，费用 834622 微单位。
