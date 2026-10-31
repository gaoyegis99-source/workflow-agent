# P4.3.2 远程执行契约

版本 `0.5.3` 开放直接远程和冻结工作流内的远程 `spatial-vector-summary@1.0.0`。客户端或工作流包只引用 Platform 管理的 `service_binding_id + binding_revision`，不能提交 URL、凭据、镜像、命令或主机路径。

工作流提交时，Platform 在写入 Action 和预算前解析精确绑定修订，并把内部快照按完整调用路径冻结。每个远程节点申请计算槽时再次核验绑定仍启用、授权仍有效且快照未变化。Execution 使用稳定节点 Action ID 建立唯一远程调用账本；上游本地节点的私有文件只有在摘要和来源唯一匹配后才能上传。公开执行查询和活动列表不返回内部绑定快照。

ServiceBinding 以项目和绑定 ID 共同确定身份。修订不可覆盖，启停状态独立保存。公开查询返回 endpoint origin、CA 引用、允许目标、限制和摘要，不返回 `credential_ref` 或密钥。每个 v2 目标冻结 contract digest 与 implementation digest。

## v2 协议

`v7-remote-execution-v2` 强制使用经指定 CA 验证的 HTTPS：

- `PUT /v1/actions/{action_id}/inputs/{port}`：检查大小和摘要后流式、原子地保存冻结输入。
- `POST /v1/actions/{action_id}/submit`：以稳定 Action ID 和请求摘要持久接受一次。
- `GET /v1/actions/{action_id}`：返回单调 revision、稳定 execution ID、执行与清理事实。
- `POST /v1/actions/{action_id}/cancel`：先持久保存取消意图，再停止并核实原容器。
- `GET /v1/actions/{action_id}/outputs/{port}`：只在成功终态提供同 Action 的受信输出。

观察字段包括 Action ID、事件 ID、远端执行 ID、revision、状态、`executed`、实现摘要、开始/结束时间、`cleanup_state` 和成功输出。Execution 将每个观察追加到历史表：同事件同内容可重放，同事件不同内容拒绝，旧 revision 不覆盖事实，`executed=true` 和终态不可回退。

状态按以下方向推进：

```text
QUEUED → STARTING → RUNNING → COLLECTING → SUCCEEDED
                              └──────────→ FAILED
任一未终态 ──取消意图与核实──→ CANCELLED
```

普通 404、连接失败、Docker inspect 失败和响应丢失保持未知，不释放预算、不更换 Action 或容器重算。远端已经开始时，额度已经转为消耗；尚未确认开始时，额度继续保留为预留。只有经过认证的 `REJECTED/CANCELLED + executed=false` 观察才释放未消耗预留。开始后失败仍计入执行消耗。

## 输入、输出与本地核验

绑定冻结输入、输出和执行时间上限。上传和下载均使用临时文件、累计大小检查、SHA-256、`fsync` 与原子改名。远程输出写入 `remote-incoming/<revision>/`；所有端口、媒体类型、大小和摘要通过后才原子提升为节点私有 `output`。重定向和跨 origin 输出地址拒绝。

远端只产生 `summary.json` 和 `method.json`。Execution 拉回后逐字段比对 Data 导入时冻结的 CRS、范围、几何类型、维度、要素数、空图层和几何有效性，并严格检查方法描述。缺端口、额外端口、非法摘要或冲突事实形成永久 `REMOTE_PROTOCOL_VIOLATION`；已声明输出的普通 404形成 `REMOTE_OUTPUT_NOT_VISIBLE` 并继续对账原远端身份。

混合工作流的子节点成果在 Execution 工作目录中保持私有。父级进入或恢复 `PUBLISHING` 时重新核对成功节点、Action/Execution、端口、摘要、来源和取消事实；只有 Data 整组提交成功后才创建资产。Data 中止先完成时父级取消且无成果；Data 提交先完成时父级成功并记录 `TOO_LATE_COMMITTED`。

`v7-remote-execution-v1` 仅为已有 `0.5.0` Action 的恢复保留。新绑定和新提交使用 v2。
