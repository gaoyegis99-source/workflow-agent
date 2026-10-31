# P7.2a / 0.12.0 部署与运行

P7.2a 没有数据库迁移。Platform 保持 `0018_agent_acceptance_identity`，Execution 保持 `0006_remote_reliability`，Data 保持 `0004_document_assets`，Knowledge 保持 `0002_lifecycle_outbox`。

## 构建与验证

```bash
uv sync --locked
bash scripts/checks/check_p7.sh --phase table-project
bash scripts/checks/check_p7.sh --phase compatibility
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`table-project` 阶段入口重建当前版本的 P3 operator、普通/GDAL checker 和 tabular operator 镜像，再运行真实 PostgreSQL、HTTP、Docker、Agent 和 Toil 闭环。不得使用直接函数调用、伪造输出或短接工作流节点替代真实门禁。

tabular 镜像摘要写入 `${V7_P7_RUNTIME_ROOT}/tabular/image-id.txt`。生成 Operator Catalog 前必须注入同一发行的不可变镜像摘要；Checker Catalog 必须包含 `tabular-project-reproduction-v1` 并指向同一发行的 checker 镜像。release lock 应包含 11 个 `0.12.0` wheel、Operator/Checker/Tool Catalog、固定镜像和源码摘要。

## 升级

```text
等待表格 Action、工作流、Acceptance 和 publication 收敛
→ 停止 Platform、Agent 与 Execution Worker
→ 安装 release lock 中全部 0.12.0 wheel
→ 构建并核验 operator、checker 和 tabular 镜像
→ 生成并核验三个 Catalog 与 release lock
→ 核对四个数据库 schema（不迁移）
→ 启动服务和 Worker
→ 运行 table-project 与 compatibility 门禁
```

Catalog 和 release lock 作为整体替换，不热更新单个算子定义。`DISABLED` 只阻止新执行并允许冻结执行恢复；`REVOKED` 继续按 P7.0 的停机、排空和整体换锁流程处理。

## 回退

先让所有 `table-project` Action、Acceptance、工作流和发布进入终态，再整体恢复 `0.11.1` wheel、Catalog、镜像和 release lock。没有数据库降级。已发布投影资产仍是不可变历史成果；`0.11.1` 不负责恢复仍在运行的 `table-project` 执行。
