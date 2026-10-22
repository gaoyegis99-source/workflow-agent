# P3 安装、运行与迁移

版本：全部 8 个发行包 `0.2.1`。Platform 迁移 `0003_p3_delivery`；Data `0001_data`；Execution `0001_execution`。旧 `0001_control`、`0002_observation_snapshot` 未修改。

## 新安装

需要 Python 3.12、uv >=0.8.14、可访问 Docker daemon 的本机运行账户，以及 PostgreSQL 17。计算 Worker 使用本机目录挂载，不能部署在与 Docker daemon 路径不同的远程机器上。容器运行账户为非 root UID；推荐直接以普通用户启动服务。

在 V7 根目录执行：

```bash
uv sync --locked
bash scripts/build_p3_images.sh
uv run python scripts/configure_p3.py
set -a
source .p3-runtime/local/p3.env
set +a
docker compose --env-file .p3-runtime/local/p3.env -f deploy/p3.compose.yaml up -d --wait
uv run platform-admin migrate
uv run data-admin migrate
uv run execution-admin migrate
uv run platform-admin check-schema
uv run data-admin check-schema
uv run execution-admin check-schema
```

配置生成器只创建一次 `0600` 凭据文件，不覆盖既有配置，不启动服务。Compose 项目名、网络和卷均为 `groundwater-v7-p3` 独立资源。数据库使用独立桥接网络，端口只发布到 `127.0.0.1`，供宿主机上的服务进程连接；计算容器另行使用 `network=none`。新 PostgreSQL 的初始化脚本只创建三个数据库和不同角色，不创建业务表；业务迁移仍由上面的显式命令执行。健康检查使用 TCP，避免初始化临时 Unix socket 服务提前报告就绪。

用管理 CLI 创建操作者与本机令牌（令牌只在签发时显示；数据库保存摘要）：

```bash
uv run platform-admin principal-create --name owner
uv run platform-admin token-issue --principal-id <上一条返回的 principal_id>
bash scripts/run_p3.sh
```

打开 `http://127.0.0.1:18070/`，输入令牌、创建项目、上传 `tests/p3/fixtures/monitoring.csv`。三个 CSV 列明确设为 string；字段角色分别为 f0/f1/f2。时区 UTC、单位 m、指标水位高程、缺失值 `["","NA"]`，分组 `["A","B","absent"]`，范围 2024-01-01 至 2024-01-05（结束不含），日周期。创建任务后 OWNER 授予精确范围，分别运行两个算子，再登记候选并请求验收。

默认服务端口为 Platform 18070、Data 18071、Execution 18072，PostgreSQL 55437，均只绑定本机。`run_p3.sh` 启动 3 个 API 和 3 个独立 Worker；任一子进程退出时停止本次脚本拥有的其余进程。实际常驻运行可由服务管理器启动相同入口并注入同一受保护配置；不允许两个不同迁移版本的进程混跑。

## P2 升级

1. 停止 V7 Platform API/Worker；保留 P2 数据库、卷、凭据和恢复材料。不要改动 V6。
2. 安装全部 `0.2.1` 包及锁定依赖，构建算子/检查器镜像。
3. 保留原 `V7_DATABASE_URL`；为 Data、Execution 单独创建数据库和角色，撤销 PUBLIC 对这些数据库的 CONNECT 权限。已有 PostgreSQL 卷不会再次执行初始化脚本，需管理员显式执行新增数据库/角色语句。
4. 配置四个不同的内部服务令牌和两个独立存储目录。Data/Execution 各自只使用自己的数据库凭据。镜像配置必须使用 `sha256:…` image ID，不能使用浮动标签。
5. 依次显式执行 `platform-admin migrate`、`data-admin migrate`、`execution-admin migrate`，然后分别 `check-schema`。升级前后保留所有任务、幂等记录、命令、授权、预算、观察快照、租约和外部关联。
6. 启动同版本的服务与 Worker。启动仅检查迁移版本，不自动迁移。

`0003` 增加派生成果/验收许可、候选、验收、完成与尝试许可关联，并允许任务 COMPLETED；旧 grant 的新增权限默认关闭。旧 P2 任务仍能查询、控制和恢复，但没有 acceptance_spec 时不能自动完成。P3 populated 表不支持降级删除；生产升级/备份回退演练属于 P8，本阶段不宣称已经验收。

## 从 P3 0.2.0 升级到 0.2.1

停止三个 V7 API 和三个 Worker，安装全部 `0.2.1` 包、更新固定算子/检查器镜像，并分别运行三个 `check-schema`。本次没有数据库迁移；Platform、Data、Execution 的迁移版本分别仍为 `0003_p3_delivery`、`0001_data`、`0001_execution`。确认配置中的镜像 ID 已更新后，再启动同版本进程。不得将 `0.2.0` 与 `0.2.1` Worker 混跑；原任务、命令、资产、预算和恢复材料保持原状。

## 存储、容器与恢复

- Data 存储目录只由 Data API/Worker 管理；原始/规范化/成果字节按 SHA-256 寻址，落盘 fsync 后才链接可见元数据。不要手工重命名或覆盖资产。
- Execution 工作目录独立；每个稳定执行拥有 input/output/runtime.lock，输入不可变。Worker 重启查询原容器并核验标签/摘要/ID，不能手工重新启动同一容器来重算。
- 容器普通用户、只读根、只读输入、独立输出、网络 none、cap-drop ALL/no-new-privileges，1 CPU、1 GiB、120 秒及 100 MiB 输出检查。容器没有服务令牌或数据库凭据。
- Worker 租约以数据库时间为准，Data 和 Execution 的长步骤每 10 秒续租 30 秒租约，代次限制回写。外部通信失败保持未知占用，1/2/4…最多 30 秒恢复重试。API/Worker 重启不清除幂等、预算、候选或恢复材料。
- 已发布成果不会因为之后取消而删除。容器停止但 Data 不可达时，清理保持 UNKNOWN。请从任务/执行查询确认事实，不以进程退出代替清理完成。
- 本阶段不自动清理历史容器、工作目录、资产和幂等记录。它们是恢复证据；保留/备份/清理策略待后续发布阶段验证。
- 已记录启动意图但无法查到原容器时，不能推断它从未执行；取消也不会自动释放未知预留。只有确认原容器从未启动，并持久保存停止意图、移除原容器及完成发布中止后，才按未执行结算。不应手动删除仍在恢复中的容器或工作目录。

## 发行与统一验收

```bash
bash scripts/check_p3.sh
```

统一入口按顺序执行锁定安装、lint/格式、固定镜像构建、真实 PostgreSQL + 独立服务/Worker + 容器/网络故障 + Chromium 浏览器测试、13 项 P1 选型规则、仓库外 8 wheel/sdist 安装和完整业务闭环，再生成 `docs/verification/p3-runtime-followup-evidence.json`。初次 `0.2.0` 验收证据及 `0.2.1` 首次补齐证据仍分别保留在 `docs/verification/p3-evidence.json` 和 `docs/verification/p3-followup-evidence.json`。测试使用临时独立角色、数据库、网络和卷，不读取部署 DSN，不需要在线业务数据。

浏览器版本随新增 Playwright 依赖锁定。当前开发主机 Ubuntu 20.04 不在 Playwright 1.63 官方平台列表，统一脚本显式使用 Ubuntu 22.04 的 Linux Chromium 构建并实际执行浏览器测试；证据记录该环境条件，不跳过测试。运行环境应提供浏览器所需系统库。

P3 统一入口回归 13 项 P1 规则；P1 重型 Toil/Weaver/gVisor 实验使用独立的 `bash scripts/check_p1.sh --record docs/verification/p1-rootless-correction-evidence.json` 执行，结果见最新复验证据。P2 历史报告和证据保留，不用 P3 结果重写历史结论。
