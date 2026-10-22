# P8.2 统一部署运行说明

P8.2 使用 `deploy/p8.compose.yaml` 部署 V7 `0.15.3`。该部署面向单机、受信 Docker 宿主，统一启动四个 API、全部后台 Worker、四个服务数据库以及恢复所需的持久目录。它不复用 V6 的代码、数据库、网络或卷。

仓库中当前激活的确切 Release digest 和实例环境文件见 [P8.2 当前运行指针](../verification/p82-current-release.json)。不要从历史报告、镜像标签或端口猜测当前发布物。

## 发布物

先构建不可变发布物：

```bash
bash scripts/build_p8_release.sh
```

发布目录不可覆盖，格式为 `.p8-runtime/releases/0.15.3-<release-digest>`，包含：

- 11 个工作区 wheel 和 64 个锁定依赖 wheel；
- Operator、Checker、Tool 三个目录；
- Agent Model Catalog、独立 Embedding Catalog 和冻结 Runtime Profile；0.15.3 的 Embedding 状态为 `DISABLED`；
- 7 个业务运行镜像的摘要，以及应用镜像和 PostgreSQL 镜像摘要；
- Capability Studio 的全部生产静态资产摘要；生产构建不生成 source map；
- schema v3 `release-lock.json`，包括 `0.15.2` 的精确恢复目录、镜像和运行配置。

`scripts/configure_p8.py` 会验证应用镜像与 Release Lock，再创建不覆盖既有目录的新实例。它为四个数据库角色和内部服务令牌分别生成凭据，凭据只写入权限为 `0600` 的实例环境文件。

```bash
uv run --locked python scripts/configure_p8.py \
  --instance production \
  --release-dir .p8-runtime/releases/0.15.3-<release-digest> \
  --gateway-bind 127.0.0.1 \
  --gateway-port 18080 \
  --execution-port 18082 \
  --postgres-port 55450

bash scripts/p8.sh up .p8-runtime/instances/production/p8.env
bash scripts/p8.sh status .p8-runtime/instances/production/p8.env
```

实例名、三个宿主端口和运行根目录必须唯一。配置器拒绝覆盖现有实例。

## 服务和所有权

| 服务组 | 进程 | 持久状态 |
|---|---|---|
| Platform | gateway、platform-api、platform-worker、authoring-worker、agent-worker | `v7_platform` 数据库、控制目录 |
| Data | data-api、table/document/spatial workers | `v7_data` 数据库、Data 内容目录、空间检查目录 |
| Execution | execution-api、supervisor、2 个 node workers、publication worker | `v7_execution` 数据库、执行目录、Toil JobStore |
| Knowledge | knowledge-api、knowledge-worker | `v7_knowledge` 数据库、Knowledge 正文目录 |

PostgreSQL 初始化脚本创建 `v7_platform`、`v7_data`、`v7_execution`、`v7_knowledge` 四个数据库和四个独立登录角色，并撤销 `PUBLIC` 对数据库的权限。服务只连接自己的数据库。

所有服务位于 Compose 私有 bridge 网络。默认只在 `127.0.0.1` 发布 gateway、Execution 回调端口和 PostgreSQL 管理端口；Data、Knowledge 和 Platform 内部 API 不直接发布。

受信局域网需要直接访问时，可将 gateway 单独绑定到全部宿主接口：

```bash
uv run --locked python scripts/configure_p8.py \
  --instance production-lan \
  --release-dir .p8-runtime/releases/0.15.3-<release-digest> \
  --gateway-bind 0.0.0.0 \
  --gateway-port 18080 \
  --execution-port 18082 \
  --postgres-port 55450
```

此设置只开放 gateway；Execution 和 PostgreSQL 仍绑定 `127.0.0.1`。应在宿主或上游网络设备上把 TCP `18080` 的来源限制到受信子网。直接访问使用明文 HTTP，不应跨越不受信网络；涉及正式凭据时，应在 gateway 前部署 TLS 反向代理。

需要启动科学容器的 Worker 只挂载 Docker socket、固定发布目录和自己的运行目录。科学容器仍使用不可变镜像、无网络、只读根目录、非 root、能力删除和资源限制。Compose 通过 Docker socket 的实际 GID 授权 Worker，避免使用 root 用户运行应用。

## 启动顺序

Compose 固定以下依赖顺序：

```text
PostgreSQL healthy
→ volume-init
→ 显式迁移四个数据库和 Agent checkpoint schema
→ 四个 API ready
→ gateway
→ Platform/Data/Execution/Knowledge/Agent/Authoring workers
```

启动不在 API 进程内自动迁移。`migrate` 必须成功退出，API 才会启动。健康检查分别验证 PostgreSQL、四个 API 和 gateway；Worker 依赖相应 API ready 后启动。

`bash scripts/p8.sh status ENV_FILE` 同时显示 Compose 服务状态和当前冷备份收敛计数。

## 停止与升级

普通停止：

```bash
bash scripts/p8.sh down .p8-runtime/instances/production/p8.env
```

升级必须通过 `scripts/p8-upgrade.sh` 执行。它先启用维护屏障并停车活动工作，再创建 schema v2 冷备份；目标 API 验证完成后才启动 Worker并开放 gateway。不要让不同 Release Lock 的 API、Worker 或科学容器混跑。完整流程见 [P8.2 维护窗口升级与回滚](p8-upgrade-rollback.md)。
