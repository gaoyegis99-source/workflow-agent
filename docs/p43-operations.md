# P4.3.2 安装、升级与运行

版本 `0.5.3` 使用 Platform `0006_service_binding_scope`、Execution `0006_remote_reliability`；Data 保持 `0003_spatial_import_runtime`。本轮没有数据库迁移，启动只检查 schema，不自动迁移。

```text
等待活动执行和发布收敛
→ 停止 API 与 Worker
→ 安装 0.5.3
→ 分别运行 Platform/Execution migrate
→ 校验三个数据库版本
→ 安装远端 CA 与 0600 Token 文件
→ 启动服务与 Worker
→ 运行真实远端 standard 和兼容回归
```

新 v2 绑定需要 `https://` origin、`tls_ca_ref` 和目标 `implementation_digest`。Execution 从以下目录解析运行材料：

- `V7_REMOTE_CREDENTIAL_DIR/<credential_ref>`：Bearer Token，必须是权限不宽于 `0600` 的普通文件，符号链接拒绝。
- `V7_REMOTE_CA_DIR/<tls_ca_ref>`：远端 CA 证书。证书主机名或 IP SAN 必须与绑定 endpoint 匹配。

`V7_REMOTE_CREDENTIALS_JSON` 只为 `0.5.0` v1 Action 恢复兼容保留。数据库、日志和公开 API 均不保存或返回明文 Token。v2 不允许明文 HTTP，也没有关闭证书校验的开关。

## 远端服务

`bash scripts/build_p43_peer.sh` 构建固定 Native GDAL 任务镜像并暂存 `deploy/p43/remote_peer.py`。peer 是受信宿主服务，需要 Docker 控制权限；该权限等价于主机管理员权限。业务任务容器不获得 Docker socket、数据库凭据或用户 Token。

peer 需要以下环境：

```text
V7_REMOTE_ROOT                  持久账本、输入和输出根目录
V7_REMOTE_TOKEN_FILE            0600 Bearer Token 文件
V7_REMOTE_JOB_IMAGE             固定 image ID
V7_REMOTE_IMPLEMENTATION_DIGEST 与绑定一致的固定实现摘要
V7_REMOTE_INSTANCE_ID           实例标识
V7_REMOTE_TLS_CERT              服务证书
V7_REMOTE_TLS_KEY               私钥
PORT                            默认 19080
```

每个 Action 使用稳定容器名。容器固定无网络、只读根目录、1 CPU、1 GiB、128 PIDs、能力全删和 `no-new-privileges`。peer 重启时读取 SQLite 账本并 inspect 原容器；Docker 暂时不可用时保持未知，不把它解释为容器不存在。

远端主机必须使用 Python 3.8 或更高版本。验收机 `192.168.167.89` 使用宿主 Python 服务和 Docker 28.1.1；任务镜像 ID及证书摘要记录在 P4.3.1a 证据中。

## 验收

```bash
bash scripts/check_p43_reliability.sh --profile quick

V7_P43_REMOTE_URL=https://remote.example:19080 \
V7_P43_REMOTE_TOKEN_FILE=/run/secrets/p43-peer \
V7_P43_REMOTE_CA_FILE=/etc/v7/remote-ca/p43-ca \
V7_P43_REMOTE_IMPLEMENTATION_DIGEST=sha256:... \
bash scripts/check_p43_reliability.sh --profile standard

bash scripts/check_p43.sh --phase direct
bash scripts/check_p432_reliability.sh --profile quick
```

跨主机 standard 还要求 `V7_P43_REMOTE_RESTART_COMMAND_JSON`。它是 JSON 参数数组，指向只能重启本轮专用 peer 的受限 SSH 命令；SSH 凭据通过环境或仓库外 `0600` 文件提供。测试会保留远端 SQLite 与工作目录，验证 peer 重启后仍定位原 Action、Execution 与容器。

回退到 `0.5.2` 前必须让含远程节点的工作流和发布全部终态。回退不需要数据库降级，不得删除仍参与预算或发布对账的 Action、节点账本、远程调用、`remote-incoming` 或私有成果。
