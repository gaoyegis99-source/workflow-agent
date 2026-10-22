# P2 补齐与重新验收报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

版本：四个发行包 0.1.1；数据库迁移 `0002_observation_snapshot`。范围限定为 P2 控制核心，未进入 P3，未修改或复用 V6 代码和运行资源。

**阶段结论：通过，结束本轮 P2；不进入 P3。** 统一入口完成于 2026-10-11T22:49:01.055980+00:00。 本报告接替 [首次验收历史报告](p2-report.md)；首次 158 项 pytest／13 项 P1 规则结果保存在 [原始结构化证据](p2-initial-evidence.json)，并不覆盖本轮后来发现的缺陷。

## 实际完成内容

公开路由、请求字段、成功状态码和 CommandReceipt 结构保持原样：项目、Task、修订、pause/resume/cancel、范围授权、预算、命令查询、health/ready/OpenAPI。没有正式计算接口或恢复测试用生产接口。

| 本轮缺陷 | 修复 | 对应回归 |
| --- | --- | --- |
| 成员降权／移除与业务写入死锁 | 项目行使用 FOR NO KEY UPDATE，兼容外键 KEY SHARE，成员变更仍串行保护最后 OWNER | `test_regressions.py` 的 6 个并发场景，6 个权限先变更场景，双 OWNER 并发移除 |
| 同版本外部回执不能退出 RECONCILING | nullable last_external_observation 区分最近外部事实与通信状态；相同事实恢复但不重复结算 | `test_regressions.py`、`test_observations.py`，真实 HTTP 查询丢响应和 404 后同版本恢复 |
| 非法执行标识写入并导致命令查询 500 | 核心标准库校验与公开 Identifier 一致；非法观察不提交；历史非法关联返回 409 INVALID_BINDING | `test_execution_identifiers.py`、`test_regressions.py`、独立 Worker 的非法回执恢复 |

相同 revision 不同事实、相同事件 ID 不同内容、身份变化、已执行事实撤回均拒绝；旧 revision 不能覆盖快照或清除未知状态；旧租约／代次不能恢复或结算。取消任务收到 RUNNING 后仍为 PENDING，未确认停止时不会显示 CLEAR。

resume 仅解除暂停，检查当前身份、项目权限和控制版本。没有有效 grant 可以恢复 ACTIVE；Action 的准入和发送分别检查要求及精确授权，因此失效的排队行动被拒绝，旧要求不能重新发送。

新增迁移只增加一个可空 JSONB 字段，`0001_control.py` 与 `schema_0001.py` 哈希和首次证据一致。旧快照初始为空，合法观察必须符合已有身份、来源版本及预留事实后才能补齐。四包版本及内部依赖统一 0.1.1；40 个第三方锁条目逐项比较保持不变。

## 开发顺序及定向验证

1. S1 固化失败：修复前正式测试复现 6 个 40P01 死锁、1 个恢复状态滞留、1 个 HTTP 500；补充非法观察不得结算测试同样失败。没有通过吞掉死锁或跳过断言消除失败。
2. S2 并发修复：13 个成员竞争／权限／最后 OWNER 测试通过，身份和控制原有 11 项通过。
3. S3 恢复与契约：182 项定向检查通过；之后包含旧库升级、观察事实、缺陷回归和身份的 34 项通过。不同轮次存在覆盖重叠，不能相加作为总测试数。
4. S4 独立 HTTP 故障：新增 11 项全部通过，48.66 秒。模拟服务和 Worker 使用独立子进程，平台使用真实 PostgreSQL。
5. S5 统一验收：以最终 `bash scripts/check_p2.sh` 输出和结构化证据为准。

定向命令：

```bash
uv run pytest -q tests/p2/test_regressions.py --tb=short
uv run pytest -q tests/p2/test_identity.py tests/p2/test_controls.py --tb=short
uv run pytest -q tests/p2/test_regressions.py tests/p2/test_actions.py tests/p2/test_observations.py tests/p2/test_identity.py tests/contracts tests/architecture --tb=short
uv run pytest -q tests/p2/test_upgrade.py tests/p2/test_observations.py tests/p2/test_regressions.py tests/p2/test_identity.py --tb=short
uv run pytest -q tests/p2/test_http_faults.py --tb=short
bash scripts/check_p2.sh
```

## 最终统一验收结果

命令：`bash scripts/check_p2.sh`，退出码 0。

| 检查 | 结果 |
| --- | --- |
| uv 依赖锁 | 45 个解析包，42 个环境包；第三方 40 个锁条目保持不变 |
| Ruff 检查及格式 | 全部通过，78 个 Python 文件 |
| pytest | **221 passed，0 failures，0 errors，0 skipped**；101.79 秒 |
| 契约 / 架构 / API 基础 / P2 | **109 / 21 / 11 / 80** 项；原 158 项覆盖保留，新增 63 项 |
| P1 规则 | **13 项通过**，重型实验未重跑 |
| 发行构建和安装 | **四个 wheel + 四个 sdist**；仓库外干净安装，核心仅依赖标准库 |
| 已安装运行验证 | 空库迁移、0001 数据升级和重复迁移；真实 API、CLI、Worker；正常／未知命令和错误封套通过 |
| Compose | 独立数据库、网络和卷配置检查通过 |
| 结构化证据 | `p2-evidence.json` 已记录，源码哈希与当前文件一致 |

保留 1 条上游 Starlette/AnyIO 弃用提示，不影响执行。没有剩余阻断 P2 验收的已知缺口；没有必测项跳过。验收后自建 P2 PostgreSQL 容器、网络、卷及临时模拟服务目录均已清理。

## 新增故障场景的证据

| 必测项 | 验证及断言 |
| --- | --- |
| 查询响应丢失 | 独立 Worker 已确认 RUNNING，模拟服务断开查询响应；保留 execution_id／快照／账目，下一进程收到同版本恢复 RUNNING，结算记录仍只有一次 |
| 普通 404 | 已知执行查询返回 404，保留身份、used 和在途名额；恢复查询仍为一个外部执行、一次 PUT，无换键重提 |
| 授权自然到期 | 用数据库时钟设置短有效期并等待自然到期；队列中到期不外发，发送窗口后到期及丢响应时保留占用，直到确凿停止后结算 |
| 无授权 resume | 通过真实 API 解除暂停；无 grant 不能准入，失效队列及旧修订均拒绝，外部调用数为零 |
| 明确拒绝 | 模拟服务持久写入 REJECTED，重启服务后记录仍在；释放一次未执行预留，后续 Worker 不再调用 |
| 普通 HTTP 错误 | 400／503 是通信未知，不等价于明确未执行；保留预留，恢复后仍为一个外部执行 |
| 非法执行身份 | 独立 Worker 收到非法 execution_id 后不写 inbox 事实／快照／结算；真实 API 命令查询仍合法，随后恢复原 Action |
| API 提交后回执丢失 | 独立测试代理完整接收上游 200 后丢弃客户端响应；SIGKILL API、重启后原键重放，只有一个 command 和一次 control_version 变化 |
| 0001 升级 | 真实旧 schema 中种入排队／未知／已知执行、任务、授权、预算、幂等、inbox、outbox 及租约；升级及重复迁移后所有旧列指纹一致，旧回执补齐快照只保留原一次扣账 |
| 发行升级 | 仓库外解释器使用安装后的 wheel，真实空库迁移和 0001 增量迁移，已知／未知 CommandReceipt、API／CLI／Worker 均验证 |

## 测试期间的问题处理

首轮故障测试遇到共享宿主磁盘同步延迟：SQLite 初始化实测约 16 秒、一次提交约 5 秒，未注入故障的请求也超过 5 秒 HTTP 超时。初次运行因此失败，未计为验收通过。

测试模拟服务改用 WAL／FULL 同步并保持连接；Linux 下其状态文件放在每个用例独立的 `/dev/shm/v7-p2-peer-*` 目录，跨模拟服务进程退出和重启保留，用例结束清理。仅测试启动等待调整到 30 秒；外部 HTTP 仍为 5 秒，正式租约仍为 30 秒／10 秒续租。平台数据库仍使用独立 PostgreSQL 容器和卷。

本轮验证的是进程终止、网络故障和增量迁移，不以模拟服务的内存文件系统证明主机断电后的存储耐久性。该限制不影响对相同 Action 身份、账户原子性和旧 Worker 隔离的断言。

## 范围与交付

- 生产不加载模拟服务；没有 Toil／gVisor、科学算子、Data、Knowledge、Agent、发布和任务页面。
- P1 组件未修改，只回归 13 项规则测试；重型组件实验未重跑。P3—P8 未执行，属于后续阶段；本轮不自动进入 P3。
- 未知外部活动继续占用预算，没有新增强制清账或自动修复历史非法身份的接口。
- 同任务行串行和单 Worker 外呼方式不变；吞吐调优、长周期容量、主机断电、生产备份恢复及回退演练仍属后续验收。
- 升级顺序：停止 V7 API／Worker → 安装 0.1.1 → 显式 migrate → check-schema → 启动新版本。不同迁移版本不得混跑，启动不自动迁移。

交付：[接口契约](../contracts/p2-control-contracts.md)、[升级与运行说明](../operations/p2-database.md)、[统一检查](../../scripts/check_p2.sh)、[最终结构化证据](p2-evidence.json)。详细运行日志与 JUnit 位于忽略目录 `.p2-runtime/verification/`，不含部署凭据。
