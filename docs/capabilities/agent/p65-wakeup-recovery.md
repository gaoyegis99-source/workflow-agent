# P6.5 / 0.8.5 持久唤醒与恢复

P6.5 将模型的 `WAIT_REQUEST` 转换为 Platform 数据库中的冻结依赖。业务事件仍全部进入 Agent 事件账本，但只有依赖达到终态或最低修订时才生成模型唤醒。因此 Execution 的排队、运行、恢复等中间状态不会重复消耗模型调用。

`agent_wait_dependencies` 保存依赖所有者、对象、最低修订、已观察修订和状态；`agent_source_watermarks` 保存各来源流的最新事件；`agent_reconciliation_timers` 在通知丢失时从权威业务事实补回唤醒。普通未知状态保持 `UNKNOWN`，既不当作成功，也不释放已有业务预算。

Agent Worker 重启时先运行 Action 与等待对账，再领取新的模型轮次。框架 checkpoint 缺失、损坏或修订不一致时，Platform 以业务账本创建新 epoch，并在 `agent_runtime_recoveries` 保存原因、前后 epoch 和账本摘要。恢复不改变 Action、Execution、成果或预算身份。

暂停继续阻止新模型与工具准入；恢复产生显式 Task 唤醒并重新检查当前权限和修订；取消会撤销租约、未消费唤醒和活动等待依赖，既有业务 Action 继续沿用 P2–P4 的真实取消与清理协议。

本轮没有扩展工具权限、科学策略或自主修复，也没有运行 P4 的长性能矩阵。
