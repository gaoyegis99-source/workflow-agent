# P4.1 R2 调度与 bridge 协议验收

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

日期：2026-10-12T13:41:56.220751+00:00。状态：**PASS**。

两组均使用 2 个 Execution Worker、嵌套条件 scatter 和固定 30 CPU 秒节点。基线使用 R1 不可变 bridge 镜像并移除生成工具的资源声明；R2 使用一次 POST、revision 长轮询以及 0.2 core / 128 MiB 协调资源。Docker init 仍关闭。

| 指标 | R1 同机基线中位数 | R2 中位数 | 变化 |
| --- | ---: | ---: | ---: |
| 工作流总耗时 | 243.521s | 149.581s | -38.6% |
| 计算窗口 | 182.017s | 93.609s | -48.6% |
| 有效计算并行度 | 1.343 | 2.617 | +94.9% |
| 计算槽利用率 | 33.565% | 65.415% | +94.9% |
| 节点 POST | 272.000 | 8.000 | -97.1% |
| 节点 HTTP 总请求 | 272.000 | 81.000 | -70.2% |
| 节点状态数据库读取 | 544.000 | 479.000 | -11.9% |
| Execution DB 连接峰值 | 5.000 | 6.000 | +20.0% |

连接峰值统计 PostgreSQL `pg_stat_activity` 中 Execution 独立数据库的客户端连接；数据库读取统计节点提交/长轮询路径对根执行和节点状态的短查询，不包含 trace 写入、Worker 领取或普通公开查询。长轮询等待期间不保留数据库事务。

R2 协议与资源门槛：**PASS**。后续综合性能门槛：**NOT_MET**。

未达到的后续性能项：
- compute window exceeds 76.5s
- parallelism is below 3.2
- slot utilization is below 80%
- eligible container start delay p95 exceeds 3s

R2 没有启用 Docker init、图压平或 job chaining；对应警告和尾部调度开销继续属于 R3-R5。
