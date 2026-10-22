# P8.1 统一部署、冷备份与恢复报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

**结论：完成态冷备份与干净恢复通过；活动工作流快照仍为明确限制。** V7 `0.15.0` 已在统一 Compose 部署中运行四个 API 和全部 Worker，使用四套独立数据库角色、私有网络、独立状态目录、冻结 Catalog/镜像和 Release Lock。冷备份在维护屏障与零未收敛工作下完成，并恢复到新的 Compose project、端口、数据库目录、状态目录和凭据中。

结构化证据见 [p81-release-evidence.json](p81-release-evidence.json)。运行说明见 [统一部署](../operations/p8-unified-deployment.md)和[冷备份与恢复](../operations/p8-cold-backup-restore.md)。

## 实际交付

- `deploy/p8.compose.yaml`：Platform、Data、Execution、Knowledge API；Platform、Data、Execution、Knowledge、Agent、Authoring Worker；PostgreSQL、初始化、迁移、gateway 和备份操作容器。
- `scripts/configure_p8.py`：创建不覆盖既有目录的实例、四套凭据和端口配置，并校验 Release Lock 与应用镜像。
- `scripts/p8.sh`：启动、状态、维护、冷备份、验证、恢复、恢复服务和停止。
- `platform-gateway`：维护期间阻止所有公开写请求，读取和健康检查保持可用。
- `platform-cold-backup`：四数据库 binary COPY、四棵状态目录、配置摘要、原子 manifest 和安全恢复。
- `scripts/build_p8_release.sh`：构建 11 个工作区 wheel、64 个锁定依赖、应用镜像和 P8 Release Lock。

Release Lock 摘要为 `sha256:e589582e…e0f78`，应用镜像为 `sha256:41414b88…9f830`。冷备份 manifest 为 `sha256:60c421c4…5a636`；其中 Platform/Data/Execution/Knowledge 共备份 98 张业务表、396 行记录，以及 Data、Knowledge、Execution/JobStore 和空间目录的 176 个文件或链接记录。

## 干净恢复结果

源部署使用 `groundwater-v7-p8-p81-source` 和端口 `18080/18082/55450`；恢复部署使用 `groundwater-v7-p8-p81-finalrestore` 和端口 `18100/18102/55470`。两者运行根、PostgreSQL 目录、四棵状态目录和凭据全部不同。恢复环境所有 API 健康，所有 Worker 正常运行。

恢复后验证了：

- P3 两个 Action、两个 Execution、预算、Deliverable、Acceptance PASS 和 Task Completion 身份保持不变；
- QGIS Buffer、Native Summary、StaticMap 三节点工作流的父 Action/Execution 和 8 个原子发布输出保持不变；
- Knowledge 精确 EvidenceRef 摘要保持不变，恢复后执行撤回得到 `WITHDRAWN`，历史精确引用仍可读取；
- AgentRun 保留 `table-profile@1.0.0` 冻结意图，恢复的计算 Action/Execution 为 `SUCCEEDED`，没有 runtime recovery 或重复扣账；
- Studio Trial、Release 和 PackageRef 保持不变；恢复后从正式 Release 新执行成功，并完成 `ACTIVE → DISABLED` 生命周期修订。

另外对 `192.168.167.89:19080` 的真实 HTTPS peer 重跑了本地 Buffer → 远端 Summary → 原子发布和直接远程计算，3 项通过。SSH 密码认证当前被远端拒绝，所以没有伪造新的跨主机 peer 重启；本机真实服务的组合重启、响应丢失、404、取消和发布竞争 25 项全部通过，既有跨主机重启 standard 证据继续保留。

## 测试结果

```text
uv run --locked pytest -q tests/p81
8 passed in 14.98s

bash scripts/check_p432_reliability.sh --profile quick
25 passed in 582.80s

真实远端 mixed workflow + direct remote
3 passed in 53.49s

uv run --locked python scripts/verify_distribution.py
11 个 wheel/sdist、锁定依赖、API、迁移、Worker、P3、CWL、QGIS、制图全部 PASS

uv run --locked ruff check .
PASS

ruff format --check（Release Lock 管理的源码目录）
316 files already formatted
```

全仓 `ruff format --check .` 会进入 `.p8-runtime` 中由容器 root 持有的 PostgreSQL/备份目录，并触发当前 Ruff 的 panic；这是运行态目录扫描问题。限定到发布源码集合后格式检查通过，运行态卷不属于源码或发行包。

## 限制与后续

当前备份是严格冷备份。门禁要求活动 Action、导入、检查、发布和 Agent 工作全部归零，因此已验证的 JobStore 是恢复材料和身份保留，不是“活动 Toil leader 跨 Compose project 继续运行”的证明。

要支持活动工作流灾备，下一轮必须先加入宿主级停车协议：停止并核验 Toil/科学容器、冻结写入、保存停车事实、恢复后按原 Action/节点/JobStore 接管，并让远程未确认调用保持 `UNKNOWN`。在这些测试完成前，不能简单放宽当前收敛查询，也不能宣称具备热备份或零数据丢失能力。
