# P7.0 收口 / 0.10.1 部署与运行

> **历史资料，不是当前部署入口。** 本页保留 lock-era 的 P7.0 操作证据；其中的
> `V7_RELEASE_LOCK` 示例不得用于新部署。当前实例请使用
> [直接 Compose 部署流程](../../operations/p8-unified-deployment.md)。

## 生产输入

0.10.1 生产模式要求以下文件来自同一次发行：

- Operator Catalog：`p70-operator-catalog.lock.json`
- Checker Catalog：`p70-checker-catalog.lock.json`
- Agent Tool Catalog：`p701-tool-catalog.lock.json`
- Release Lock：`p701-release-lock.json`
- release lock 中列出的 10 个 wheel 和六个镜像摘要

至少设置：

```bash
export V7_DEPLOYMENT_MODE=production
export V7_RELEASE_LOCK=/etc/v7/p701-release-lock.json
export V7_RELEASE_DIGEST=sha256:e3a1277122922d6b462cc4efd836dc00039fbcdcc4544951d9b28c5769afde0e
export V7_OPERATOR_CATALOG=/etc/v7/operator-catalog.json
export V7_CHECKER_CATALOG=/etc/v7/checker-catalog.json
export V7_AGENT_TOOL_CATALOG=/etc/v7/agent-tool-catalog.json
```

Execution 还必须设置与 release lock 完全相同的 `V7_OPERATOR_IMAGE`、`V7_SPATIAL_QGIS_IMAGE_ID`、`V7_SPATIAL_NATIVE_IMAGE_ID`、`V7_CHECKER_IMAGE`、`V7_GDAL_CHECKER_IMAGE` 和 `V7_CWL_IMAGE_ID`。缺失、浮动标签、目录篡改、目录错配、版本错配或镜像摘要错配都会阻止启动或 readiness。

## 升级顺序

0.10.1 没有数据库迁移。升级采用整体停机，避免旧新目录混跑：

```text
停止新任务和 Agent 准入
→ 等待活动计算、工作流、检查器、AgentRun 和发布收敛
→ 停止 Platform、Execution、Agent 及相关 Worker
→ 安装 release lock 指定的 0.10.1 wheel
→ 部署同一 release 的三个 Catalog 和六个镜像
→ 核对现有 Platform/Data/Execution/Knowledge schema
→ 启动 Execution，再启动 Platform 和 Agent Worker
→ 检查三方 release/catalog 摘要和 readiness
→ 恢复准入
```

旧 P6 AgentRun 没有 P7 目录绑定。升级前应让它们进入终态；确需恢复时使用封存的旧 Tool Catalog 和旧 Worker，不能交给 0.10.1 P7 Worker。0.9.3 及更早计算执行仍通过兼容目录读取；历史终态记录不会被重写。

## 生命周期变更

本轮不支持热撤销。将算子改为 `DISABLED` 或 `REVOKED` 时，必须执行同样的停止准入、排空、停机、生成新目录和新 release lock、整体启动流程。

- `DISABLED`：新执行不再准入。
- `REVOKED`：新启动和未开始的发布也被拒绝。
- 已经原子发布的资产不删除，历史 Action、预算、报告和 provenance 保持可读。

若存在无法收敛的活动执行，不得切换生命周期 release；先按该执行原 release 完成取消或恢复。

## 回退

回退前同样排空 0.10.1 活动运行。停止服务后整体恢复 0.10.0 的 wheel、目录、镜像和 release lock。不得只回退一个服务，也不得用 0.10.0 Worker 推进已经冻结 P7 v2 目录的新 AgentRun。数据库无需降级，历史记录不得删除。

## 验证

```bash
bash scripts/checks/check_p7.sh --phase catalog
bash scripts/checks/check_p7.sh --phase compatibility
```

发行验证还应在仓库外的 Python 3.12 环境安装 release lock 中全部 wheel，运行 `uv pip check`，再启动真实 Platform、Execution 和 Agent 组合。项目明确要求 Python `>=3.12,<3.13`。
