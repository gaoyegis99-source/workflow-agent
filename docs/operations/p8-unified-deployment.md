# V7 直接 Compose 部署与维护

V7 的 P8 运行态使用项目自己的 rootless Docker daemon 和直接 Compose
配置。它不读取 `release-lock.json`、`V7_RELEASE_LOCK` 或历史 P8 身份观察值；部署
标识只是用于追踪和动态科学容器标签的元数据，不是启动门禁。

旧 Release Lock、历史发布目录和验证材料不属于当前工作区，也不能作为服务配置输入。

## 配置实例

先准备一个位于 V7 项目内的只读 artifact 目录，包含 Operator、Checker、Tool、Model
Catalog 和 Agent Runtime Profile。应用镜像和七个科学镜像必须由维护者显式提供；配置器只检查
本项目 Docker socket 和镜像可用性，不会启动 daemon 或回退到宿主 Docker。

```bash
bin/configure-p8 \
  --instance production \
  --artifact-dir /home/mtd/liweigis/workflow/groundwater-v7/runtime/active/artifacts/production \
  --app-image groundwater-v7-app:production \
  --operator-image sha256:<operator> \
  --checker-image sha256:<checker> \
  --gdal-checker-image sha256:<gdal-checker> \
  --spatial-qgis-image sha256:<qgis> \
  --spatial-native-image sha256:<native> \
  --tabular-image sha256:<tabular> \
  --cwl-image sha256:<cwl>
```

该命令创建不可覆盖的 `runtime/p8/instances/<instance>/`。其中 `compose.env` 只包含
路径、端口、镜像和拓扑参数；每个服务独立的 `environment/<service>.env` 为 `0600`，只包含
该服务所需的数据库 URL、Token、目录和 Catalog。Gateway 不会获得数据库密码。

默认仅 Gateway 发布宿主端口，并默认为 loopback。要置于受信 TLS 代理之后，可选择
`--identity-mode trusted-proxy --trusted-proxy-token-file <0600 文件>`；代理仍须剥离客户端
伪造身份头，服务端仍以 Principal 和项目成员关系授权。

## 拓扑与最小权限

| 网络 | 成员与用途 |
| --- | --- |
| `v7-service` | Gateway、API 与普通 Worker 的内部 HTTP |
| `v7-database` | PostgreSQL、迁移和需要各自数据库的服务 |
| `v7-workload` | Execution API 与动态 CWL 容器；内部网络 |
| `v7-egress` | 仅 Agent、Knowledge 等明确需要 Provider/Embedding 外连的服务 |

所有常规应用容器使用应用镜像中的非 root 用户、只读根文件系统、最小 `/tmp`、`cap_drop: ALL`
和 `no-new-privileges`。PostgreSQL、卷初始化、迁移和冷备容器的可写或 root 权限有显式
例外标签。

只有 package lane、authoring worker、data-spatial worker、Execution
supervisor/node/publication 以及 backup 挂载 V7 自己的 Docker socket。action、acceptance 和
knowledge lane 没有 Docker group、socket 或 Execution 工作目录。动态 CWL 容器只加入
`v7-workload`，使用 `http://execution-api:8012` 回调，从不使用宿主网络或 Docker socket。

## 维护顺序

任何会改变运行态的动作都要由维护者明确执行，顺序固定如下：

```text
bin/p8 preflight
→ maintenance-on
→ cold backup + verify
→ 停止写入服务
→ 更新或新建实例配置
→ bin/p8 up
→ readiness 与实际任务/产物验证
```

```bash
env_file=runtime/p8/instances/production/compose.env
bin/p8 preflight "$env_file"
bin/p8 maintenance-on "$env_file"
bin/p8 backup "$env_file" runtime/p8/instances/production/backups/<run-id>
bin/p8 verify "$env_file" runtime/p8/instances/production/backups/<run-id>
bin/p8 up "$env_file"
bin/p8 status "$env_file"
```

`bin/ops release plan|verify|apply --env-file <compose.env>` 是相同流程的人工入口；`apply`
仍要求 `--execute`。运行时脚本只通过 V7 的 `bin/docker` / `bin/compose` 访问项目 socket，拒绝
Docker target、context 或 TLS 覆盖。socket 不可用时命令返回阻断，不会启动 daemon 或连接宿主
Docker。

锁驱动构建和升级命令只返回明确的退役信息；当前维护流程不读取旧锁或旧发布目录。
