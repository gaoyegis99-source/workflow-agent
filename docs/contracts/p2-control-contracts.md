# P2 控制核心契约

本文件描述实际实现。P0 的 `TaskRef`、`AssetRef`、计算目标、`CommandReceipt` 和 `ErrorEnvelope` 保持兼容；P2 新增项目、任务、授权、预算与控制请求 DTO。完整类型由 `uv run --locked python -m platform_contracts.schema` 导出；实际 HTTP 路由见 `/api/v1/openapi.json`。内部 `ActionRequest` 不作为公开计算接口导出。

## 身份、项目与请求

所有业务请求使用 `Authorization: Bearer <token>`，不接收客户端自行声明的操作者身份。无效／撤销／过期令牌返回 401；无项目成员关系返回 404，避免泄露项目存在性；成员角色不足返回 403。项目创建者自动成为 OWNER。VIEWER 只读，EDITOR 可创建和控制任务，OWNER 另外可管理范围授权。成员管理经 CLI，并要求当前 OWNER 的令牌。

每个改变状态的 POST 必须提供 `Idempotency-Key`：1～128 个可见 ASCII 字符，不含空白。范围是 **principal_id + project_id（创建项目时 GLOBAL）+ operation + key**。摘要使用校验后的规范化 JSON，包含目标资源；忽略字段顺序、补齐 DTO 默认值，时间统一 UTC。相同范围内同键异内容返回 409；同键同内容返回原资源或命令关联。换令牌不改变操作者范围，但重放仍重新检查令牌和当前角色。错误事务回滚，不占用请求键。

幂等响应保存首次接受时的快照，查询接口返回当前状态。命令回执的 `request_id` 每个 HTTP 请求重新生成；`command_id`、`action_id` 和原始关联稳定。P2 不清理幂等、命令、授权撤销、预留或恢复记录。

所有请求对象禁止额外字段；整数不接受布尔或字符串转换。输入校验失败 400；明确不支持的 AGENT 模式 422；版本／状态／额度冲突 409；数据库暂时不可用 503，`retry_mode=SAME_KEY`。错误统一 `ErrorEnvelope`，不返回 SQL、令牌、输入值或内部异常。401 带 `WWW-Authenticate: Bearer`。

## 实际接口

令 `P=/api/v1/projects/{project_id}`，`T=P/tasks/{task_id}`。

| 方法与路径 | 最低权限 | 请求／返回 | 成功状态 |
| --- | --- | --- | --- |
| GET `/api/v1/health` | 无 | 进程存活，phase=P2 | 200 |
| GET `/api/v1/ready` | 无 | 数据库可用且版本为 `0002_observation_snapshot` | 200；未就绪 503 |
| GET `/api/v1/openapi.json` | 无 | 已实现路由及错误契约 | 200 |
| POST `/api/v1/projects` | 有效身份 | `{"name":"项目名"}` → ProjectView | 201 |
| GET `/api/v1/projects` | 有效身份 | 当前成员项目分页 | 200 |
| GET `P` | VIEWER | ProjectView | 200 |
| POST `P/tasks` | EDITOR | TaskCreate → TaskView | 201 |
| GET `P/tasks`、GET `T` | VIEWER | TaskPage／TaskView | 200 |
| POST `T/spec-revisions` | EDITOR | 两个预期版本与新 spec → TaskView | 201 |
| POST `T/pause`、`T/resume`、`T/cancel` | EDITOR | `expected_control_version` → CommandReceipt | 200 |
| GET `P/grants` | VIEWER | GrantPage | 200 |
| POST `P/grants` | OWNER | GrantCreate → GrantView | 201 |
| POST `P/grants/{grant_id}/revoke` | OWNER | 无请求体 → GrantView | 200 |
| GET `T/budget` | VIEWER | BudgetView | 200 |
| GET `P/commands/{command_id}` | VIEWER | CommandReceipt | 200 |

列表使用 `limit`（默认 50、范围 1～100）和不透明 `cursor`；返回 `items`、`next_cursor`。按稳定 ID 排序，游标绑定项目及资源类别，跨范围复用返回 400。分页不是跨请求一致性快照；并发新建可能需要从首屏重新查询。

P2 公开控制命令在事务内完成，返回 200；取消响应完成的是任务控制转换，外部停止由 `cleanup_status` 单独表达。异步 Action 的内部准入返回 `RECORDED`，将来公开接受此类命令时才使用 202；本轮不存在用模拟服务实现的 `/executions` POST。

## Task、要求与授权

创建示例：

```json
{
  "execution_mode": "DIRECT",
  "spec": {"goal": "验证任务控制与恢复", "requirements": ["保留要求修订记录"]},
  "budget": {"execution_count": 5, "max_in_flight": 2}
}
```

要求是用户文本，尚不代表科学分析协议或验收标准。`goal` 1～4000 字符，requirements 最多 32 项，每项 1～200 字符。预算必须显式给出：执行总次数 0～1,000,000，最大在途数 1～1000；P2 无调整接口。

任务保存独立的 `spec_revision`、`control_version`、`grant_epoch`；初始均为 1。任务生命周期仅 OPEN／CANCELLED；控制为 ACTIVE／PAUSED；清理为 CLEAR／PENDING／UNKNOWN。没有完成／成功状态接口。

- 修订同时检查 `expected_spec_revision` 与 `expected_control_version`，追加不可变要求记录，并使两个版本递增。普通执行观察、预算结算及清理进度不改变控制版本。
- 暂停拒绝新 Action，并停止尚未发送的提交；已进入发送窗口的活动继续跟踪。恢复只解除暂停，检查当前身份、项目角色及控制版本。没有有效计算 grant 也可以恢复为 ACTIVE；恢复不授予计算许可。Action 在准入和发送前分别检查要求版本及精确范围，失效的待发送 Action 被拒绝。重复同状态的**新键**命令返回 409，原键则正常重放。
- 旧修订下尚未发送的 Action 拒绝并释放未执行额度；在途 Action 仍关联原修订。请求、目标、资源引用、CLIENT 决定来源及独立 frame_id 冻结保存。
- 取消立即阻止新准入，增加控制版本与授权代次，标记活动清理。CLEAR 代表平台已取得无剩余活动的依据；UNKNOWN 代表仍有无法确认的活动。CANCELLED 本身不表示计算已停。

GrantCreate 必须给出 task_id、targets、resources 和带时区的 expires_at。targets 为 1～32 个**精确计算目标**，resources 为最多 128 个精确 AssetRef（可为空）；一个有效 grant 必须覆盖整个 Action，不能拼接不完整的多个 grant 扩大权限。授权不确认资源存在或 manifest 内容正确。

GrantView 另含 grant_id、project_id、created_by、创建时 generation、revoked、revoked_at、revoked_by。授予／撤销递增任务授权代次。尚未发送的 Action 代次不匹配时保守拒绝，需以新键重新准入；在途活动继续查询，失去有效覆盖时尝试取消。授权变更不会改写旧 Action。撤销与过期均不能消除已进入发送窗口的外部副作用。

## 预算与可靠投递

预算查询字段：execution_count、max_in_flight、used、reserved、in_flight。始终保持 `used + reserved <= execution_count`、`in_flight <= max_in_flight`，所有值非负。

| 已确认事实 | 总次数账目 | 并发占用 |
| --- | --- | --- |
| Action 持久准入 | reserved +1 | in_flight +1 |
| 外部确认已经执行 | reserved -1，used +1（只一次） | 保留至停止 |
| 未发送即拒绝／外部封存为未执行 | reserved -1，不增加 used | -1 |
| 已执行且确认终止 | used 保留 | -1 |
| 超时、普通 404、网络／数据库故障 | 不凭缺失证据释放 | 保留 |

准入把 Action、预算预留、幂等结果、Command 投影与 ACTION outbox 写在同一事务；每个 Action 唯一预留。确认账目变化时在同一事务追加 budget_settlements、更新预留、登记 inbox、更新命令观察。Command 只描述意图是否关联到外部执行，RESOLVED 不是“计算成功”或“任务完成”。

身份／成员检查使用共享锁；API 准入先身份与幂等记录，再任务锁；Worker 发送复核先身份／成员，再任务、outbox。涉及同任务的范围、控制、预算变更均在任务锁下串行。成员 CLI 先取得项目 `FOR NO KEY UPDATE` 锁，再身份及成员锁；继续串行保护成员变更和最后 OWNER，同时兼容业务外键检查的 `FOR KEY SHARE`。竞争中请求先完成或权限变更先完成后请求被拒绝均合法，不依赖死锁重试。预算行无需额外逆序加锁。行锁的事务语义依据 [PostgreSQL 17 文档](https://www.postgresql.org/docs/17/explicit-locking.html)。

Worker 使用短事务 `FOR UPDATE SKIP LOCKED` 领取到期 outbox，保存 owner、generation、lease_until；外部 HTTP 在提交事务后执行。此跳过锁行为只用于队列领取，依据 [PostgreSQL SELECT 文档](https://www.postgresql.org/docs/17/sql-select.html)。TaskEvent outbox 被投递为本地审计事件；Action outbox 驱动提交／查询／取消，不新增消息中间件。

默认租约 30 秒，每 10 秒续租；测试 HTTP 适配器超时 5 秒。时钟来自数据库；续租和回写都要求 owner＋generation＋未过期匹配。旧 Worker 可能完成其已发出的 HTTP，但不能回写权威账目。外部提交永远使用原 Action ID。

发送意图先持久化；首次 HTTP 丢失响应后进入 RECONCILING。按原 Action ID 查询，普通未找到不是释放证据。若仍有权限、原要求有效且未暂停，可使用**同一 Action ID**幂等补发；暂停期间保留未知占用，取消后请求外部封存。已有 execution_id 的 Action 即使查询未找到也不换键提交。

失败重查间隔为 1、2、4、8、16、30 秒，之后上限 30 秒；合法且不旧于最近事实的观察重置连续故障计数。正常在途状态每秒查询。来源事件按 source＋event_id 去重，同事件异内容拒绝，旧 revision 不覆盖新事实；明确 REJECTED／SEALED 停止重试。所有未知观察保留可查询记录及不含载荷的日志。

## 外部事实、恢复状态与标识符

Action 的可空 `last_external_observation` 保存最近合法的 action_id、revision、execution_id、state、executed，不含 event_id。网络失败仅把运行状态置为 RECONCILING，保留该快照和身份；Command 已有关联时仍可返回 RESOLVED，表示身份已定位，并非活动已停止。

| 观察 | 处理 |
| --- | --- |
| 更高 revision、合法事实 | 原子登记 inbox、更新快照、Action、Command 和必要结算 |
| 同 revision、同事实（同事件或新事件） | 恢复观察状态，清零连续失败，不重复结算 |
| 同 revision、不同事实 | INVALID_BINDING，事务回滚，不写账目 |
| 较旧 revision | 不覆盖事实，不解除 RECONCILING，不清零失败数 |
| 同 source/event_id、不同内容 | INVALID_BINDING，事务回滚 |
| 失效租约／代次 | 不回写权威状态 |

恢复后按任务取消状态重新计算清理进度。已取消任务收到 RUNNING 时清理为 PENDING，不是 CLEAR；通信失败时为 UNKNOWN。

ExecutionPort 的 Action ID 和 Execution ID 必须与公开 Identifier 一致：`[A-Za-z0-9][A-Za-z0-9_-]{0,127}`，ASCII、完整匹配。核心以标准库校验类型、字符、长度，应用再验证 Action 归属及执行身份稳定性。Event ID 独立为长度 1～128 的不透明字符串，允许冒号。不会裁剪、替换或生成另一个外部身份。

非法观察不能写入 inbox 事实、快照或预算结算；Worker 保留未知占用，记录 Action、事件、代次与错误类别后继续恢复，不记录令牌或请求载荷。历史非法 execution_id 的命令查询返回 `409 INVALID_BINDING` 的 ErrorEnvelope，原身份和账目保留；无效调用方仍先经过身份及项目权限检查。

升级前已有记录快照为空。相同 revision 的合法观察须与现有执行身份、预留 charged/stopped 和已知状态一致才可补齐；RECONCILING 不能被用来伪造历史状态。更旧的回执不能补齐快照。

## 实现限制

当前 ExecutionPort 只有测试 HTTP 适配器；正式 Worker 不加载它。模拟服务通过自己的 SQLite 状态实现幂等和取消封存，平台状态始终使用 PostgreSQL。未来执行后端必须证明稳定提交身份、可靠查询及取消封存／终止证据，不能仅把任意 HTTP 404 解释为“未执行”。

本阶段未做生产吞吐压测、长期日志保留治理、多区域数据库、备份升级回退演练或公网身份网关；这些不影响本轮控制协议验证，但不能据此声称平台已达到 P8 发布条件。
