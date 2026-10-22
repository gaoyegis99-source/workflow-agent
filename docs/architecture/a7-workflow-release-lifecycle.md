# Capability Studio A7 / 0.14.6 工作流版本生命周期契约

## 1. 目的与边界

A7 在 A6 的不可变工作流 Release 上增加可审计的运行治理。Release 的 source、Package、试运行证据和 SemVer 仍不可修改；生命周期只决定是否允许新的正式执行、既有执行是否可以启动新节点，以及尚未提交的成果是否可以发布。

A7 不删除版本，不修改历史 Action 或成果，不增加跨项目共享、marketplace、Agent 编写工作流或科学验收捷径。

## 2. 状态和修订

生命周期状态为：

- `ACTIVE`：允许新的正式 Release 执行。
- `DISABLED`：临时阻止新执行；已接受 Action 可以按原冻结身份恢复、启动节点并发布。
- `REVOKED`：永久阻止新执行；已接受 Action 继续对账，但不得取得新的节点许可或首次发布许可。

合法转换为：

```text
ACTIVE ──→ DISABLED ──→ ACTIVE
   │            │
   └────────────┴────→ REVOKED
```

`REVOKED` 是终态。转换必须由项目 OWNER 提交，携带当前 `expected_lifecycle_revision` 和非空原因。同一旧修订上的并发请求只有一个可以成功。

每次转换写入不可变 `workflow_release_lifecycle_events`；Release 行只保存当前投影。升级前已有 Release 回填为 `ACTIVE / revision 1 / INITIAL_RELEASE`，原 source、Package、Trial 和时间戳保持不变。

## 3. 首次准入与幂等重放

正式执行仍使用 A6 接口。首次请求按以下顺序处理：

1. 读取精确 Definition、SemVer 和 Package；
2. 在事务外解析 Data 输入并构建 ExecutionPlan 与 EnginePackage；
3. 最终事务锁定 Task 和 Release；
4. 重查 Release 身份、`ACTIVE` 状态和生命周期修订；
5. 原子写入预算、Action、Command、outbox 和工作流额度。

Action 的 `_workflow_release` 额外冻结准入时的生命周期状态和修订，作为来源事实。生命周期在准备期间变化时，最终事务拒绝准入且不留下 Action 或预算预留。

已成功接受的同键同内容请求先恢复原幂等结果。因此 Release 后来被停用或撤销时，客户端仍能找回原 Command，而不会创建第二个 Action。同键异内容继续返回冲突。

## 4. 既有执行与节点许可

每个工作流节点取得计算槽前，Platform 使用当前 Release 行重新核对冻结 Release 身份：

- `ACTIVE`、`DISABLED`：既有 Action 可以继续；Task、grant、预算、算子和远程绑定仍须分别通过原检查。
- `REVOKED`：尚未获许可的节点得到 `RELEASE_REVOKED`，不启动容器、不取得计算槽。
- 已经开始的节点：保留实际执行和预算事实，继续查询或取消，不声称撤销消除了外部副作用。

生命周期变化不会切换 Package、节点 Action、Execution、镜像或成果身份。

## 5. 发布许可仲裁

A7 增加 `workflow_release_publication_permits`。Execution 在 Data 已准备但尚未提交资产时向 Platform 请求发布许可。发布许可和生命周期转换锁定同一 Release 行，因此只有两种确定结果：

- **许可先提交**：该 Action 获得不可变 `ALLOW`。随后即使 Release 被撤销，这次既有发布仍可使用原许可完成；这表示发布权已经先发生。
- **撤销先提交**：该 Action 首次申请得到不可变 `DENY / RELEASE_REVOKED`，不得发布。

许可按 Action 唯一，重试和 Worker 接管返回同一决定。算子当前生命周期仍在每次调用时独立检查；Release 许可不能绕过算子撤销。Data 已经原子提交的资产保持可见，任何后来的生命周期变化都不删除事实。

## 6. 接口

新增：

```text
GET  /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/
     releases/{version_label}/lifecycle-revisions

POST /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/
     releases/{version_label}/lifecycle-revisions
```

POST 请求：

```json
{
  "expected_lifecycle_revision": 1,
  "lifecycle": "DISABLED",
  "reason": "等待输入数据修订完成"
}
```

Release 查询增加 `lifecycle`、`lifecycle_revision` 和 `lifecycle_reason`。历史 Release 始终可查询；`current_executable` 只有在生命周期为 `ACTIVE` 且原有环境、Package、算子和远程绑定检查也通过时才为真。

## 7. 安全与限制

- VIEWER 可读取版本和生命周期历史；只有 OWNER 可变更生命周期。
- 生命周期记录、发布许可、Release、Action 和成果均不提供删除接口。
- `DISABLED` 是运营暂停，不用于紧急阻止已接受计算；需要永久阻止新节点和未许可发布时使用 `REVOKED`。
- `REVOKED` 不等于取消。若要请求停止已经运行的容器，仍使用 Task 或 Execution 取消协议。
- `SAMPLE_VERIFIED` 仍只表示冻结样例通过，不等于科学 `VERIFIED`。
