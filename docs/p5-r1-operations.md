# P5-R1 / 0.7.0 运行、升级与回退

升级前等待活动验收、执行和发布收敛，然后停止 Platform API 和 Worker。安装九个 `0.7.0` 发行包，显式将 Platform 升级到 `0009_output_identity`；Data、Execution、Knowledge 分别保持 `0004_document_assets`、`0006_remote_reliability`、`0002_lifecycle_outbox`。启动仍只检查 schema，不自动迁移。

迁移为已有 Deliverable 写入 `identity_mode=LEGACY`，并为 Acceptance 增加可空的 `analysis_registration_id`。迁移不修改旧 payload、CoveragePlan、报告、Completion、资产 manifest 或执行账本。

重建固定运行镜像并核对源码标签：

```bash
bash scripts/build_p3_images.sh
bash scripts/build_p4_image.sh
bash scripts/build_p42_images.sh
uv run platform-admin migrate
uv run platform-admin check
bash scripts/check_p5.sh --phase identity
bash scripts/check_p5.sh --phase all
```

统一门禁覆盖精确绑定篡改、同名端口、scatter/数组位置、显式登记、被替代登记、`0008→0009` 升级、仓库外安装、真实 PostgreSQL 和固定 checker 容器，并回归 P0–P5-R0。

回退到 `0.6.3` 前必须停止创建 R1 Deliverable 和 Acceptance，并确保没有 R1 验收在途。由于 `0.6.3` 不理解精确角色，正常回退需要恢复升级前数据库备份；仅执行 Alembic downgrade 会删除 R1 身份模式和登记选择列，不能用于保留已经形成的 R1 科学审计历史。
