# P7.1a / 0.11.0 部署与运行

P7.1a 没有数据库迁移。Platform 保持 `0018_agent_acceptance_identity`，Execution 保持 `0006_remote_reliability`，Data 保持 `0004_document_assets`，Knowledge 保持 `0002_lifecycle_outbox`。

## 构建与验证

```bash
uv sync --locked
bash scripts/build/build_p71a_image.sh
bash scripts/checks/check_p7.sh --phase tabular
bash scripts/checks/check_p7.sh --phase catalog
bash scripts/checks/check_p7.sh --phase compatibility
```

独立镜像摘要写入 `${V7_P7_RUNTIME_ROOT}/tabular/image-id.txt`。生产环境必须把该值设置为 `V7_TABULAR_OPERATOR_IMAGE`，并用同一次发行的四类计算镜像生成 Operator Catalog：

```bash
export V7_OPERATOR_IMAGE=sha256:...
export V7_SPATIAL_QGIS_IMAGE_ID=sha256:...
export V7_SPATIAL_NATIVE_IMAGE_ID=sha256:...
export V7_TABULAR_OPERATOR_IMAGE="$(cat ${V7_P7_RUNTIME_ROOT}/tabular/image-id.txt)"
uv run python scripts/build/build_operator_catalog.py generate \
  --output /etc/groundwater-v7/operator-catalog.json
```

release lock 必须包含 11 个 `0.11.0` wheel、Operator/Checker/Tool Catalog、七个镜像摘要和源码摘要。少一个 wheel、多一个 wheel或任一摘要不一致都会拒绝生成或使 readiness 失败。

## 升级

```text
等待活动 Action、AgentRun 和 publication 收敛
→ 停止 Platform、Agent 与 Execution Worker
→ 安装 release lock 中全部 0.11.0 wheel
→ 构建并核验 tabular 镜像
→ 生成并核验三个 Catalog 和 release lock
→ 核对四个数据库 schema（不迁移）
→ 启动 Data、Execution、Platform、Agent 和 Worker
→ 运行 catalog、tabular、compatibility 门禁
```

生产目录不热更新。`DISABLED` 阻止新执行但允许已冻结执行恢复；`REVOKED` 的变更仍采用 P7.0 的停机、排空、整体换锁流程。

## 回退

先让两个表格算子的 Action、工作流和发布进入终态，再整体恢复 `0.10.1` 的 wheel、Catalog、镜像和 release lock。没有数据库降级。已经发布的 `OPERATOR_RESULT` 仍是不可变历史资产；`0.10.1` 不负责推进尚未终态的 `TABULAR_V1` 执行。
