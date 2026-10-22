# P4.1 R4.4 Worker 队列拆分验收

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

日期：2026-10-12T20:05:14.252936+00:00。状态：**PASS**。

部署为 1 个 supervisor、8 个 node 和 1 个 publication Worker；supervisor 保留
publication 对账能力。同时运行两条
flat_v1 工作流，每条包含固定 30 CPU 秒节点。公开 API、预算和节点身份不变。

| 指标 | 拆分前 2 Worker | 拆分前 4 个通用 Worker | 角色拆分 |
| --- | ---: | ---: | ---: |
| 叶子领取 p95 最差值 | 1031.770ms | 456.222ms | 162.167ms |
| 两工作流总耗时中位数 | — | 86.531s | 84.982s |
| 节点 POST | — | 16 | 16 |
| 进程组警告 | — | 0 | 0 |

work_class 是 Execution 自有的持久列和领取索引；Worker 不解析 JSON 来选择工作。
P3 仍可使用默认 all 角色，P4 的启动脚本固定使用角色布局。
