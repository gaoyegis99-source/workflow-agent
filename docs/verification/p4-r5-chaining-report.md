# P4.1 R5 Toil chaining 对照

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

日期：2026-10-12T22:23:49.026097+00:00。

- 运行逻辑：**PASS**。
- 性能资格：**DEFERRED**（原门槛返回 FAIL，未放宽）。
- 正式选择：**KEEP_CHAINING_OFF**。

两组使用相同 `flat_v1`、角色 Worker 池和固定 30 CPU 秒节点，唯一变量是 Toil `--disableChaining`。standard 按 `off→on→on→off→off→on` 各运行三次，并先通过 5 项 chaining-on 真实恢复与取消测试。

| 指标 | chaining off | chaining on | on 改善 |
| --- | ---: | ---: | ---: |
| 总耗时中位数 | 84.173s | 91.249s | -8.406% |
| 计算窗口中位数 | 62.743s | 64.706s | -3.129% |
| 并行度中位数 | 3.909 | 3.936 | — |
| 槽位利用率中位数 | 97.713% | 98.400% | — |
| 领取 p95 最差值 | 1788.040ms | 1226.371ms | — |
| 节点 POST | 每次 8 | 每次 8 | 重复 0 |
| 进程组警告 | 0 | 0 | — |

同机仓库外测试使两组各有一次领取 p95 超过 500ms，因此原性能门槛正确把本组标为 FAIL。所有功能完整性事实均通过：固定长节点、冻结 profile、稳定身份、8 次 POST、trace 完整、预算精确结算以及零重复、零进程警告。

当前要求先收口逻辑，性能复测延后。没有可靠证据达到 10% 收益，生产配置继续使用 `toil-single-host-v2-chaining-off`。性能恢复为验收目标时，必须完整重跑 standard，不能使用本报告启用 chaining。
