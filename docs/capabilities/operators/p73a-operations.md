# P7.3a / 0.13.0 部署与运行

P7.3a 没有数据库迁移。Platform 保持 `0018_agent_acceptance_identity`，Execution 保持 `0006_remote_reliability`，Data 保持 `0004_document_assets`，Knowledge 保持 `0002_lifecycle_outbox`。

## 构建与门禁

```bash
uv sync --locked
bash scripts/checks/check_p7.sh --phase table-aggregate
bash scripts/checks/check_p7.sh --phase catalog
bash scripts/checks/check_p7.sh --phase compatibility
uv run --locked python scripts/checks/verify_distribution.py
```

`table-aggregate` 入口重建当前版本的 operator、普通/GDAL checker 和 tabular operator 镜像，再运行真实 PostgreSQL、HTTP、Docker、Agent、checker 和三节点 Toil 工作流。正式闭环不能用直接函数调用、伪输出或短接工作流节点替代。

Operator Catalog 必须包含 `table-aggregate-native-v1`；Checker Catalog 必须包含 `tabular-aggregate-reproduction-v1`。Platform、Execution 和 Agent 必须核对同一 Operator Catalog 摘要，Platform 与 Execution 必须核对同一 Checker Catalog 摘要。release lock 精确包含 11 个 `0.13.0` wheel、三个 Catalog、七个不可变镜像摘要和源码身份。

## 升级

```text
等待表格 Action、Acceptance、工作流和 publication 收敛
→ 停止 Platform、Agent 与 Execution Worker
→ 安装 release lock 中全部 0.13.0 wheel
→ 构建并核验 operator、checker 与 tabular 镜像
→ 生成并核验 Operator/Checker/Tool Catalog 与 release lock
→ 核对四个数据库 schema（不迁移）
→ 启动服务和 Worker
→ 运行 table-aggregate、catalog 与 compatibility 门禁
```

Catalog、镜像和 release lock 必须整体替换，不能只更新聚合规则。`DISABLED` 阻止新执行并允许既有冻结执行恢复；`REVOKED` 使用 P7.0 的停机、排空和整体换锁流程。

## 回退

先让所有 `table-aggregate` Action、Acceptance、工作流和发布进入终态，再整体恢复 `0.12.2` wheel、Catalog、镜像和 release lock。数据库无需降级。已发布聚合成果继续作为不可变历史资产读取；`0.12.2` 不能恢复正在运行的 Aggregate 执行。
