# P2 独立数据库、身份与迁移

本阶段使用 PostgreSQL 17、SQLAlchemy 2.0、psycopg 3、Alembic，完整解析版本见根目录 `uv.lock`。Compose 的镜像固定为 `postgres@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826`；不跟随可变的 latest 标签。

## 建立本机环境

从 V7 根目录执行。以下步骤创建自己的数据库和随机凭据；不要填写 V6 的连接串，也不要引用其网络或卷。

```bash
uv sync --locked
source scripts/lib/runtime-layout.sh
umask 077
mkdir -p "$V7_P2_RUNTIME_ROOT/local"
uv run --locked python - <<'PY'
import os
from pathlib import Path
import secrets
path = Path(os.environ['V7_P2_RUNTIME_ROOT']) / 'local/database.env'
password = secrets.token_hex(32)
with path.open('x') as output:
    output.write('V7_POSTGRES_PASSWORD=' + password + '\n')
    output.write('V7_POSTGRES_PORT=5547\n')
    output.write('V7_DATABASE_URL=postgresql+psycopg://v7:' + password
                 + '@127.0.0.1:5547/v7_control\n')
PY
set -a
source "$V7_P2_RUNTIME_ROOT/local/database.env"
set +a
../bin/compose -f deploy/platform/p2.compose.yaml up -d --wait
uv run --locked platform-admin migrate
uv run --locked platform-admin check-schema
```

重复启动时只需 source 原文件；创建脚本使用独占创建，不会覆盖已有密码。若修改端口，需同步修改连接串。Compose 项目名为 `groundwater-v7-p2`，数据库名 `v7_control`，使用自己的 `control` 网络和 `control-data` 卷，仅绑定 127.0.0.1。密码文件位于忽略目录且由 umask 限制权限；不提交到仓库。

```bash
uv run --locked platform-api --host 127.0.0.1 --port 8007
# 另一个配置相同环境变量的终端：
uv run --locked platform-worker --lane action
```

Worker 按 `package`、`action`、`acceptance`、`knowledge` 四个 lane 独立部署和退避；每个进程必须通过 `--lane` 选择唯一职责。Action lane 默认租约 30 秒、每 10 秒续租；生产入口不连接模拟计算服务。`platform-worker --lane <lane> --once` 至多处理一次对应 lane 的可领取工作并退出。未配置数据库时 Worker 拒绝启动；API 可启动供存活检查，但 ready 为 503。若配置了数据库，API／Worker 必须检查到匹配迁移版本才启动。进程退出不会取消任务；未完成 outbox 在租约过期后可接管。

## 本机令牌与成员管理

持有专用数据库凭据的本机管理员可创建操作者、签发／撤销令牌。令牌为 `v7_` 前缀加 32 随机字节的 URL-safe 编码；数据库仅保存 SHA-256 摘要。仅 `token-issue` 输出明文，请自行保管。默认有效 30 天，可通过 `--ttl-seconds` 指定 1 秒至 365 天。

```bash
uv run --locked platform-admin principal-create --name owner
# 从上条 JSON 取 principal_id，设置 PRINCIPAL_ID 后运行：
uv run --locked platform-admin token-issue --principal-id "$PRINCIPAL_ID"
# 从签发 JSON 取 token_id，撤销时运行：
uv run --locked platform-admin token-revoke --token-id "$TOKEN_ID"
```

业务 HTTP 统一 Bearer Token。创建项目后，操作者成为该项目 OWNER。成员变更必须同时有本地数据库访问权和当前 OWNER 令牌；CLI 从 `V7_OWNER_TOKEN` 环境变量读取，不把令牌作为命令行参数。授权规则仍在事务内复核，EDITOR／VIEWER 无法提权。

```bash
# 在终端中设置 V7_OWNER_TOKEN、PROJECT_ID、MEMBER_ID。
uv run --locked platform-admin member-set \
  --project-id "$PROJECT_ID" --principal-id "$MEMBER_ID" --role VIEWER
```

角色支持 OWNER、EDITOR、VIEWER，REMOVE 表示移除成员。禁止移除最后一名 OWNER。普通访问中，无成员关系返回 404，成员角色不足返回 403。CLI 为本机管理工具，没有另建公开身份管理 API；生产外部身份接入不在 P2 范围。

## 迁移与持久材料

当前发行版本为 **0.1.1**，迁移头为 `0002_observation_snapshot`。初始迁移 `0001_control` 保持原样，新增迁移只为 actions 增加可空 JSONB `last_external_observation`。Alembic schema 快照随 infrastructure wheel 一同发行。`platform-admin migrate` 显式执行 upgrade head，并使用数据库事务级 advisory lock 串行迁移；重复执行安全。API／Worker 不运行 migration，不自动纠正版本漂移。就绪检查同时检查数据库可连接与迁移版本匹配。

持久对象分组：

| 所有者 | 表 |
| --- | --- |
| 身份／项目 | principals、tokens、projects、members |
| Task | tasks、task_specs（追加修订）、task_events（投递后的审计） |
| Authorization | grants（精确范围、有效期、创建代次、撤销者和时间） |
| Budget | budgets、reservations（Action 唯一）、budget_settlements（结算记录） |
| Action／Command | actions（冻结请求、CLIENT 来源、发送意图、外部观察）、commands（查询投影） |
| 可靠投递 | idempotency、outbox（租约／代次）、inbox（来源事件去重） |

预算唯一性、非负与额度上限由数据库约束兜底；规则和原子事务由核心用例执行。未知外部状态保留预留、并发占用和恢复资料。数据库暂时不可用时 API 返回安全 503；Worker 持续重试，连接池重新检查连接。不要手工删除 outbox、inbox、幂等键或预留来“解卡”。

本轮验证独立空库安装及含任务、预算、幂等、租约和待恢复 Action 的 `0001_control` 升级；逐项核对全部旧列保持不变，重复迁移安全。没有从 V6 搬迁运行数据。后续迁移应新增修订文件，不修改已交付的初始 schema 快照；业务备份、恢复、生产升级和回退演练留在 P8，本轮不声称已完成这些验收。

## 从 0.1.0 升级至 0.1.1

按以下顺序执行，不混跑不同迁移版本的进程：

1. 停止本实例 V7 API 和全部 Worker，保留数据库、网络及卷。确保由服务管理器启动的进程不会自动拉起旧版本。
2. 安装本次四个 0.1.1 发行包，或在 V7 工作区执行 `uv sync --locked`；保留第三方依赖锁定版本和原数据库凭据。
3. 显式执行 `uv run --locked platform-admin migrate`，再执行 `uv run --locked platform-admin check-schema`，应匹配 `0002_observation_snapshot`。
4. 启动新 API／Worker，检查 `/api/v1/health`、`/api/v1/ready` 和原命令查询。旧 outbox 仍按原身份及租约代次恢复。

迁移不清除或重算历史状态。旧快照初始为空，等待与既有来源版本、身份及预留一致的合法观察补齐；未知状态不得凭推测补写。历史非法执行标识不会自动更名，查询明确返回 409 INVALID_BINDING。不要删除旧账目来规避该错误。

启动仍不自动迁移。生产备份、恢复和回退演练属于 P8；本次只验收 P2 的增量迁移及进程恢复协议。

## 运行验收

```bash
bash scripts/checks/check_p2.sh
```

检查入口主动移除继承的 `V7_DATABASE_URL`／`V7_OWNER_TOKEN`，自动建立随机名称的数据库容器、凭据、网络、卷。只对这些自建测试资源注入 stop/start，HTTP 故障服务与 Worker 位于独立进程。平台用真实 PostgreSQL 独立卷；模拟外部服务自己的 SQLite 文件不是平台数据库替代品。Linux 下测试模拟服务使用每个用例独立的 `/dev/shm/v7-p2-peer-*` 目录、WAL 和 FULL 同步，文件跨模拟进程 SIGKILL／重启保留，用例结束后清理。这样隔离共享宿主磁盘延迟对 5 秒 HTTP 故障时点的干扰；本测试不证明主机断电后的外部服务持久性。

测试覆盖并发额度、重复请求、修订／控制竞争、事务提交前后 SIGKILL、外部接受后丢失响应、API／Worker／模拟服务重启、SIGSTOP 旧 Worker 接管、取消封存和晚到提交、心跳续租、乱序观察、数据库停机恢复。时间较长的接管测试会将测试 outbox 租约推进到数据库当前时间，再验证真实第二进程接管；不把缩短的测试等待描述为默认 30 秒租约的计时精度测试。

验收结束清理测试资源，不启动长期运行的业务部署。`$V7_P2_RUNTIME_ROOT/verification/` 保存本次测试结果；需要长期留存时上传外部制品系统，不在源码树保存副本。结果不包含密码或令牌。

补齐测试另覆盖：成员降权／移除与三类业务写入竞争，同版本观察恢复、冲突及过期代次，授权自然到期、无授权恢复、持久拒绝、普通 404 恢复，真实 API 回执经测试代理丢弃后重启重放，以及已安装发行包的旧库升级和未知命令查询。
