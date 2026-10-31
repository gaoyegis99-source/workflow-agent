# V7 命令目录

从 `source/` 运行分类命令；项目一级 `bin/` 只保留稳定的 Docker、Compose、P8 和人工维护入口。
旧的平铺 `scripts/<command>` 路径不再提供兼容包装。

日常人工维护从项目根开始：

```bash
bin/ops status
bin/ops check fast
bin/ops check changed
bin/ops check changed --plan
bin/ops identity whoami
bin/ops release plan --env-file runtime/p8/instances/production/compose.env
bin/ops cleanup plan --run-id 2026-10-23-1
bin/ops migrate exact-identity plan --run-id 2026-10-24-1
bin/ops runtime retire-p8 plan --run-id 2026-10-24-1
```

`check changed` 自动运行本地检查；只有带 `--execute` 才运行可能创建隔离测试资源的
集成检查。`runtime backup|restore|start|stop`、`identity bind` 和 `release apply` 都要求
`--execute`。P8 使用项目内直接 Compose 配置；发布级检查要求存在经预检的 `compose.env` 与
项目自己的 Docker socket，但不会回退到宿主 Docker。

`migrate exact-identity` 是业务身份兼容退役的唯一入口。`plan` 只盘点并写入
`operations/maintenance/exact-identity/<运行号>/`；无法从权威角色绑定和 manifest schema
确定迁移结果时整批 `BLOCKED`。维护窗停写并完成可校验冷备后，才可执行：

```bash
export V7_EXACT_IDENTITY_BACKUP_DIR=/项目内/runtime/下的已验证冷备目录
bin/ops migrate exact-identity apply --run-id 2026-10-24-1 \
  --backup-id sha256:冷备清单摘要 --execute
bin/ops migrate exact-identity verify --run-id 2026-10-24-1
```

该命令不推断角色、不改写资产 manifest、不接受任意 SQL；P8 目录和旧路径兼容不属于此迁移。

`identity bind` 还必须提供已有 Principal、稳定 IdP subject、维护者标识和变更原因；命令在数据库中
追加不可变绑定审计事件，且终端摘要不会回显 subject 或原因。

## 旧 P8 运行态退役

`runtime retire-p8` 是唯一的 P8 安全退役入口。它没有 P8 根、Docker 根、容器、卷或路径参数；所有候选都来自同一运行号的不可覆盖清单。真实执行前必须把一套**项目外**、专用于该运行号的数据库、对象存储、制品和配置冷备完成恢复演练，并提供不含密钥值的外部密钥托管回执。

```bash
# 只盘点；不会启动 Docker 或移动 P8 内容。
bin/ops runtime retire-p8 plan --run-id 2026-10-24-1

# backup-root 与回执必须在项目外；验证记录只保存摘要和覆盖范围。
bin/ops runtime retire-p8 backup-verify --run-id 2026-10-24-1 \
  --backup-root /verified-external/v7-p8-2026-10-24 \
  --secret-escrow-receipt /verified-external/v7-p8-escrow-receipt.json

bin/ops runtime retire-p8 stage --run-id 2026-10-24-1 --execute
bin/ops runtime retire-p8 verify --run-id 2026-10-24-1
bin/ops runtime retire-p8 purge --run-id 2026-10-24-1 --scope metadata --execute
bin/ops runtime retire-p8 purge --run-id 2026-10-24-1 --scope docker-data --execute
```

`stage` 只复制已登记的非敏感发布物、验证/日志材料及实例的脱敏元数据到 `runtime/archive/p8-retirement/<run-id>/`；不复制任何 secrets。任一不可读路径、符号链接、运行容器、哈希变化、冷备/恢复/托管证明缺失或不匹配都会失败关闭。Docker 数据只能经 V7 rootless socket 以精确 ID 处理；不允许手工删除数据根、宿主回退或全局 `prune`。Docker 镜像始终只盘点，不删除。


## 检查结果与执行边界

`check changed` 默认执行注册表选择的本地检查；不会安装依赖、启动服务或覆盖正式构建。
`--execute` 才运行集成与发布检查，`--plan` 完全不运行检查，两者互斥。唯一执行注册表为
[maintenance-gates.v2.json](maintenance-gates.v2.json)，包含路径归属、命令和前置条件。
Git 查询失败、无归属的活跃改动和损坏的注册表均明确失败，不按“没有改动”处理。

检查汇总按 `FAILED → BLOCKED → PARTIAL/PLANNED → PASS` 判定。只有全部必需检查实际通过
才返回 `PASS/0`；失败返回 1，缺少前置条件或发布阻断返回 75，待执行返回 2。
显式 `--plan` 成功生成未阻断计划时返回 `PLANNED/0`，不表示检查已通过。
这些退出码不改变清理盘点因 Docker 不可用而返回 `PARTIAL` 的协议。

`check fast` 只验证源码结构和项目路径契约。完整运行态、已装配指针及恢复条件仍由发布检查验证；
干净克隆没有这些对象时不能宣称发布就绪。继承的 Docker context 不会覆盖本项目 socket，
不存在的 socket 不会触发守护进程启动或宿主回退。

## 运行态瘦身

`cleanup` 是唯一的本地可再生产物清理入口。其兼容入口、通用机制和 V7 策略分别位于
`cleanup_core.py`、`cleanup_policy.py`、`cleanup_rebuild.py`；它不接受目录、通配符或任意路径；先由 `plan` 写入
项目根的 `operations/maintenance/cleanup/<日期>/` 审计目录，再只按该清单中的候选 ID 操作。
审计 JSON 不记录环境变量、Token 或密钥，已有同名运行目录和记录绝不覆盖。

```bash
# 在 groundwater-v7/ 项目根执行；plan 只盘点，不移动任何对象
bin/ops cleanup plan --run-id 2026-10-23-1

# 人工审阅 inventory/reference/hash 后，才可隔离一个明确 ID
bin/ops cleanup stage --run-id 2026-10-23-1 \
  --candidate v7.cache.frontend-node-modules --execute
bin/ops cleanup verify --run-id 2026-10-23-1 \
  --candidate v7.cache.frontend-node-modules

# 自动隔离重建并生成绑定证明；不恢复原候选路径
bin/ops cleanup rebuild --run-id 2026-10-23-1 \
  --candidate v7.cache.frontend-node-modules --execute

# 永久删除是独立操作；隔离移动本身不计入磁盘释放
bin/ops cleanup purge --run-id 2026-10-23-1 \
  --candidate v7.cache.frontend-node-modules --execute
```


文件候选在 `verify` 后必须运行 `cleanup rebuild --execute`。固定重建步骤只写本项目的
`runtime/work/cleanup-validation/` 临时目录：缓存执行回归与快速检查，前端执行锁文件安装、
类型检查、测试和临时输出构建，装配使用独立生成根。不从审计中的命令字符串执行 Shell。
失败及成功证明均追加保存；证明绑定清单、隔离记录、源码和锁文件指纹。源码或锁文件变化需重新验证。
`purge` 再次检查引用、保护规则、Git 忽略状态、原路径未重现和隔离哈希；旧版完整性 PASS
本身不授权删除。无登记重建步骤的旧构建、工具链和工作目录继续不可清理。Docker 仍走独立状态机。

隔离位置固定为 `runtime/archive/cleanup-quarantine/<日期>/`。候选会在计划、隔离和删除前重复检查
SHA-256、文件数、权限、链接、项目边界和活动引用；候选根链接、越界链接、硬链接、受引用路径和受保护
路径均拒绝。npm 的 `.bin` 内部相对链接仅在目标仍位于该 `node_modules` 树内时允许，并且清理器从不
跟随它们。

当前可自动隔离的范围仅为前端依赖树、测试/Lint 缓存和 Python 字节码。`runtime/work/p3`、`p4`、`p7`
与 `runtime/toolchain/p1-toil` 只盘点，必须先提供锁文件重建和能力验证证明。`runtime/toolchain/v7`、
Docker 数据、卷、数据库、对象存储、密钥、活动配置和 `runtime/p8` 一律排除。Docker 只通过
V7 自己的 socket 做受控盘点；socket 不可用时该子阶段停在 `UNAVAILABLE`，不会启动守护进程或触碰
数据根。缺少有效 direct-Compose 控制文件时 Docker 候选仍会标记为不可处置；预检成功后也只允许
“已停止且没有 bind/volume 挂载”的容器和无引用悬空镜像走
`stage → verify → purge --execute`。`stage` 只冻结元数据，最终删除使用精确对象 ID 的
`docker container rm` 或 `docker image rm`，绝不传递 `-v`、删除卷或执行 build-cache prune。

| 目录 | 职责 |
| --- | --- |
| `scripts/build/` | 构建镜像、目录和运行配置 |
| `scripts/checks/` | 能力门禁、发行和静态检查 |
| `scripts/configure/` | P2/P3/P8 配置生成 |
| `scripts/evaluation/` | Agent 评测与黄金任务 |
| `scripts/lib/` | 共享路径解析与无副作用库 |
| `scripts/maintenance/` | 布局、卫生、依赖和归档审计 |
| `scripts/operations/` | P3/P4/P8 生命周期操作 |
| `scripts/release/` | 历史发布审计与 artifact 清单；锁驱动构建入口已退役 |
| `scripts/verification/` | 只读探测、报告和验收记录辅助命令 |

推荐先用项目级工具链执行 Python 命令：

```bash
runtime/toolchain/v7/bin/python source/scripts/maintenance/check_internal_layout.py
runtime/toolchain/v7/bin/python source/scripts/maintenance/check_project_hygiene.py
```

P8 入口在 `bin/configure-p8` 与 `bin/p8`；它们只接受项目内 `runtime/p8/instances/*/compose.env`
和 V7 rootless Docker socket。详见[直接 Compose 部署说明](../operations/p8-unified-deployment.md)。
