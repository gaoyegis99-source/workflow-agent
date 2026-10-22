# P4.3.1a / 0.5.1 远端可靠性补齐实施计划

## 1. 目标与边界

基于 `0.5.0`，把直接远程执行从正常路径样机提升为可恢复的单机受信执行服务：

**冻结输入 → 远端持久接受 → 每 Action 独立容器 → 查询/取消/恢复 → 限量回收 → 本地独立核验 → 原子发布。**

本轮只修复直接远程 `spatial-vector-summary@1.0.0`。不接入远程 CWL 节点，不实现多远端调度、跨主机高可用、自动故障转移、P5 Knowledge 或 Agent，也不设置性能优化门槛。

`v7-remote-execution-v1` 只用于恢复已有 `0.5.0` Action。新提交使用 `v7-remote-execution-v2`，不得将新语义静默套入旧协议。

## 2. 权威状态与远端运行模型

远端持久状态固定为：

```text
UPLOADING → QUEUED → STARTING → RUNNING → COLLECTING → SUCCEEDED
                          └──────────────→ FAILED
UPLOADING/QUEUED/STARTING/RUNNING/COLLECTING → CANCELLING → CANCELLED
```

- Action ID、请求摘要、输入摘要、实现摘要和容器名称首次接受后不可改变。
- 调度器通过条件更新领取：只有 `QUEUED AND cancel_requested=false` 才能进入 `STARTING`。
- 稳定容器名由 Action ID 派生。创建、启动或 inspect 响应丢失后只定位原容器，禁止换名重算。
- `executed=true` 只在 Docker 证明原容器已经开始后设置，且不可回退。
- 取消先持久保存意图，再检查容器。只有 inspect 证明未启动或已经停止后才能形成终态。
- peer 重启时逐项核对原容器；不得把全部 `RUNNING` 无条件改为失败。
- Docker 暂时不可查询时保持 UNKNOWN 和预算占用，不猜测未执行或已停止。

每个 Action 启动一个固定 Native GDAL 容器。远端控制服务作为受信宿主服务访问 Docker；运维文档明确 Docker 控制权限等价于主机管理员权限，不通过把 Docker socket 暴露给业务容器来隐藏该权限。

固定容器限制：

- 1 CPU、1 GiB、128 PIDs、120 秒、100 MiB 输出。
- 非 root、只读根目录、只读输入、独立输出、`--network none`。
- `--cap-drop ALL`、`no-new-privileges`、固定 seccomp、有限 `/tmp` tmpfs。
- 固定 image ID 和标签：Action ID、请求摘要、实现摘要、远端代次。
- 不挂载数据库、服务 Token、Docker socket或其他 Action 目录。

## 3. 协议、契约与安全

### v2 观察事实

v2 观察在 v1 字段上增加：

- `implementation_digest`：必须匹配冻结绑定。
- `started_at`、`finished_at`：UTC 时间；未发生时为 null。
- `cleanup_state`：`NOT_REQUIRED | PENDING | CONFIRMED | UNKNOWN`。
- `outputs` 只允许成功终态出现，并继续限制为 Action 同源相对路径。

Execution 新增只追加的远端观察历史，以 `(action_id, event_id)` 去重。同事件不同内容拒绝；旧 revision 不覆盖事实；同 revision 冲突拒绝；执行事实和终态不可回退。

### 绑定与凭据

- 在标准库核心模块中建立唯一的远端绑定校验函数，管理 CLI 和应用服务共同使用。
- 完整检查 endpoint、credential ref、targets、contract digest 和三项 limits。
- ServiceBinding 主键改为 `(project_id, service_binding_id)`，与项目作用域接口一致。
- 新凭据从 `V7_REMOTE_CREDENTIAL_DIR/<credential_ref>` 读取，要求普通文件、权限不宽于 `0600`；环境 JSON 仅用于恢复旧执行。
- 新 v2 绑定只允许 HTTPS。验收环境使用本地 CA，为 `192.168.167.89` 签发含 IP SAN 的证书；生产部署不得关闭证书校验。
- bearer token 仅作应用认证；服务端证书负责端点身份。mTLS 和集中密钥服务留到后续多远端阶段。

### 传输限制

- 上传前检查冻结 `input_bytes`；远端先检查 Content-Length，再以临时文件流式接收、计算摘要、fsync 后原子改名。
- 下载前检查端口集合、媒体类型、摘要格式和声明大小；以流式响应写临时文件，边下载边计算摘要并执行累计上限。
- 任何单端口或累计输出超过 `output_bytes` 时立即断开并拒绝，不先读入内存。
- `timeout_seconds` 从远端容器实际启动时计算。超时触发持久取消；容器停止得到确认后记录 `FAILED/TIME_LIMIT`，未知时保持清理 UNKNOWN。
- 429、503 和网络错误按瞬时故障处理；明确 4xx 契约错误停止重试。普通 404 始终保持未知，不能证明未执行。

## 4. 结果核验与发布

- `summary.json` 必须与 Data 导入阶段冻结的选中图层事实逐字段一致，包括 CRS、范围、几何类型、维度、要素数、空图层和几何有效性。
- `method.json` 必须精确匹配算子 ID、版本和允许的方法描述；额外字段按严格契约拒绝。
- 文件摘要验证、科学/空间事实验证和成果描述符生成完成后，才能进入本地 Data 发布。
- 远端不得持有 Platform、Data、用户或发布凭据，不能直接创建资产。
- 发布失败继续使用原 Execution ID 和发布键对账，不重新执行远端计算。

## 5. 分步实施

| 步骤 | 实施内容 | 通过条件 |
| --- | --- | --- |
| S1 | 固化取消竞争、重启、响应丢失、超限和超时失败用例 | 修复前能够稳定复现已确认问题；测试不使用生产故障开关 |
| S2 | v2 契约、统一绑定校验、项目复合主键和观察历史迁移 | 正反契约、旧 v1 恢复、空库与 `0.5.0` 升级通过 |
| S3 | 远端状态机、条件领取、稳定容器和恢复对账 | 排队/启动/运行取消不偷跑；peer 重启不换身份 |
| S4 | 容器隔离、限额、流式上传下载和执行截止时间 | Docker inspect 证明全部限制；大输入/输出不会进入内存 |
| S5 | HTTPS、文件凭据和严格结果核验 | 明文 v2、错误证书、宽权限密钥、伪造结果全部拒绝 |
| S6 | 真实跨机故障矩阵、发行安装、完整兼容回归和报告 | 必测项无失败或跳过，发布 `0.5.1` 后结束本轮 |

迁移：

- Platform `0006_service_binding_scope`：绑定和修订使用项目复合键，原记录原样迁移。
- Execution `0006_remote_reliability`：增加观察历史、截止时间和必要的远端诊断事实。
- Data 保持 `0003_spatial_import_runtime`。

## 6. 必测场景

真实 `192.168.167.89` Docker 环境必须覆盖：

- 取消发生在 QUEUED 读取后、容器创建前、创建后启动前、运行中、收集和发布期间。
- 提交被远端接受但响应丢失；查询临时 404 后恢复；输出下载中断后继续原文件。
- Execution Worker、远端 peer 和任务容器分别终止并恢复。
- Docker inspect 暂时不可用、容器创建响应丢失、旧远端代次迟到回写。
- peer 重启后定位原容器；运行容器不得重复，已完成容器不得重算。
- 超时、OOM、PIDs 上限、超大输入、声明超大输出、流式实际超限。
- 错摘要、错媒体类型、额外文件、错误 method、伪造要素数/范围/CRS。
- 绑定禁用发生在上传前、上传后提交前和开始后；权限与预算事实正确。
- Token 轮换保留旧 Action 恢复能力；错误证书和错误 Token 拒绝。
- 两个以上 Action 并发时目录、容器、账本和结果不串扰。
- 同一幂等键仍只有一个 Platform Action、一个远端容器、一次预算结算和一组成果。
- P3 科学闭环、P4.1 工作流、P4.2 空间/制图和 `0.5.0` v1 待恢复执行兼容。

故障代理和远端管理探针只放在测试目录。测试通过受控 SSH 管理精确标签的测试容器，不把任意 Docker 控制接口加入生产 HTTP 服务。

## 7. 验收入口与结束条件

新增：

```bash
uv run --locked pytest -q tests/p431a
bash scripts/check_p43_reliability.sh --profile quick
bash scripts/check_p43_reliability.sh --profile standard
bash scripts/check_p43.sh --phase direct
bash scripts/check_p4.sh --phase cartography
bash scripts/check_p4.sh --phase spatial
bash scripts/check_p4.sh --phase workflows
bash scripts/check_p3.sh
```

`quick` 验证部署、HTTPS、单次执行、运行取消和 peer 重启。`standard` 运行全部跨机故障矩阵。两者都使用真实远端 Docker、固定算子镜像和真实 PostgreSQL；不得使用模拟远端、伪输出或短接发布。

结束条件：取消不再偷跑，远端任务具备独立容器和固定限制，响应丢失及三类进程重启不重复计算，超限数据不会被完整读入内存，伪造空间结果不能发布，HTTPS 和文件凭据生效，完整兼容回归通过。达到后交付 `0.5.1` 并结束，不自动进入远程工作流或 P5。
