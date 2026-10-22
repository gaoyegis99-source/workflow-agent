# P4.1 R1 工作流时间线、事件字典与指标

R1 建立于 `0.3.1`；事件字典已核对至 `0.3.8`。本文件描述 Execution 自有的诊断事实和长节点测试。诊断信息不参与任务状态、预算、授权、恢复或发布决策，也不通过公开 HTTP API 查询。

## 存储与安全边界

Execution 迁移 `0003_workflow_trace` 新增只追加表 `workflow_trace_events`。`event_key` 是父执行、节点、来源、事件类型和确定性区分值的 SHA-256；重复事件使用 `ON CONFLICT DO NOTHING`。`workflow_execution_id + observed_at` 和 `node_action_id + observed_at` 建有查询索引。

R4.4 的 `0004_execution_work_class` 在 `executions` 增加受约束的 `work_class` 和 `(work_class, available_at)` 待领取索引。该字段参与 Worker 领取，但不改变 trace、节点身份、预算或公开状态。

每次写入使用独立短事务。数据库暂时不可用时只写结构化告警，业务状态保持原样；恢复后新事件继续记录。表没有更新、删除或 HTTP 查询接口。降级到 `0002_workflows` 只删除该观测表，不修改执行、节点、预算、JobStore 或成果。

属性采用事件级白名单，只允许标量。禁止令牌、请求体、输入值、路径、资产内容以及任意嵌套对象。`observed_at` 使用 PostgreSQL `clock_timestamp()`，用于持久排序；`source_monotonic_ns` 来自同一主机内核的单调时钟，用于跨本机进程和容器计算阶段差值，不转换为实际日期。

## 事件字典

| 来源 | 事件 | 节点范围 | 允许属性 | 含义 |
| --- | --- | --- | --- | --- |
| Platform client | `PERMIT_REQUESTED` | 父/节点 | `phase` | 开始向 Platform 请求许可 |
| Platform client | `PERMIT_ALLOWED` / `PERMIT_WAIT` / `PERMIT_DENIED` | 父/节点 | `decision` | 许可结果；`duration_ms` 是内部 HTTP 耗时 |
| Engine | `ENGINE_CREATED` / `ENGINE_STARTED` / `ENGINE_EXITED` | 父 | `container_id`，退出时可含 `exit_code` | Toil leader 容器生命周期 |
| Bridge | `BRIDGE_STARTED` | 节点 | `bridge_instance_id` | 受信 bridge 进程开始 |
| Bridge | `ENGINE_BINDING_OBSERVED` | 节点 | `kind`、`binding_id` | 可空 Toil Job ID 的诊断关联；不参与节点身份或业务判断 |
| Execution API | `NODE_ACCEPTED` | 节点 | `created` | 首次持久接受稳定节点 Action |
| Execution Worker | `EXECUTION_CLAIMED` | 节点 | `phase` | 首次领取已接受节点 |
| Execution Worker | `CONTAINER_CREATED` / `CONTAINER_STARTED` / `CONTAINER_EXITED` | 节点 | `container_id`，退出时可含 `exit_code` | 真实科学容器生命周期 |
| Execution Worker | `OUTPUT_VERIFIED` | 节点 | `file_count` | 输出摘要和 manifest 已核实 |
| Execution Worker | `NODE_OBSERVATION_REPORTED` | 节点 | `revision`、`state` | 节点观察已回写 Platform |
| Execution API | `NODE_TERMINAL_OBSERVED` | 节点 | `bridge_instance_id`、`revision`、`state` | bridge 看见终态观察 |
| Bridge | `BRIDGE_RETURNED` | 节点 | `bridge_instance_id`、`revision` | bridge 已核实并交出 CWL 输出 |
| Engine | `GATHER_FINISHED` | 父 | `member_count` | 冻结输出完成来源核对和收集 |
| Data client | `PUBLICATION_PREPARED` / `PUBLICATION_COMMITTED` / `PUBLICATION_ABORTED` | 父 | `state` | Data 原子发布仲裁阶段 |
| Execution API | `DIAGNOSTIC_PROBE` | 父 | `sample` | 性能测试测量单次 trace 写入开销 |

每条事件还可携带 `engine_epoch` 和 `worker_generation`。旧 epoch 或旧 generation 的业务写入仍由既有租约和代次规则拒绝；trace 只是保存已发生的诊断事实。

## 指标公式

- **接受到领取：** `EXECUTION_CLAIMED.monotonic - NODE_ACCEPTED.monotonic`。
- **许可 HTTP：** `PERMIT_*` 事件的 `duration_ms`。
- **许可到容器启动：** `CONTAINER_STARTED.monotonic - PERMIT_ALLOWED.monotonic`。
- **容器结束到 bridge 返回：** `BRIDGE_RETURNED.monotonic - CONTAINER_EXITED.monotonic`。
- **上一容器结束到下一节点接受：** 对每次节点首次接受，取此前最近的容器退出时间并求差。
- **引擎启动到首次提交：** 首个 `NODE_ACCEPTED - ENGINE_STARTED`。
- **最后节点返回到 gather/发布：** `GATHER_FINISHED - 最后 BRIDGE_RETURNED`，以及 `PUBLICATION_COMMITTED - GATHER_FINISHED`。
- **计算窗口：** 最早科学容器启动到最晚科学容器退出。
- **平均计算并行度：** 所有科学容器运行秒数之和 ÷ 计算窗口秒数。
- **槽位利用率：** 平均计算并行度 ÷ 声明的 Platform 并发上限。本轮上限固定为 4。
- **容器启动间隔：** 按启动时间排序后，相邻科学容器启动时间之差。
- **trace 开销：** 当前执行事件数 × 32 次独立探针写入的中位毫秒数 ÷ 工作流总毫秒数。该值是保守估算，不从业务耗时中扣除。
- **协调内存候选：** `max(bridge p99 RSS, tagger p99 RSS) × 1.5`，向上取整到 32 MiB 档，且不得超过 128 MiB。

## 固定诊断矩阵

`quick` 为 4 个固定 30 CPU 秒节点，每个配置一次。`standard` 为 8 个固定 30 CPU 秒节点、Platform 并发上限 4，每个配置三次。两者都保持 Toil `--maxCores 2`、现有 bridge 协议、现有编译图、`--disableChaining true` 和生产 Worker 配置不变。

```bash
bash scripts/check_p4_scheduler_diagnostics.sh --profile quick
bash scripts/check_p4_scheduler_diagnostics.sh --profile standard
```

R1 当时的包装器可以启动 1/2/4 个共享数据库和工作目录的 Execution Worker，并在 D 组比较 Docker init。自 `0.3.3` 起生产 Toil 容器固定启用 init，测试代码不再提供开关；R1 原始证据保持不变。所有科学容器的实测运行时间必须至少 25 秒；任何短节点都会使诊断失败。

最终 standard 结果和原始事件见[结构化证据](verification/p4-workflow-scheduler-diagnostics.json)，可读结论见[诊断报告](verification/p4-workflow-scheduler-diagnostics-report.md)。R1 只决定 R2/R3 参数，不声称达到 3.2 并行度或 100 秒总耗时门槛。
