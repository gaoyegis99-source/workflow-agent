# P5-R1 / 0.7.0 运行、升级与回退

升级前等待活动验收、执行和发布收敛，然后停止 Platform API 和 Worker。安装九个 `0.7.0` 发行包，显式将 Platform 升级到 `0009_output_identity`；Data、Execution、Knowledge 分别保持 `0004_document_assets`、`0006_remote_reliability`、`0002_lifecycle_outbox`。启动仍只检查 schema，不自动迁移。

当前 schema 头为 `0026_worker_lane_leases`。升级前必须使用
`bin/ops migrate exact-identity plan` 证明全部 Deliverable 已有确定的角色绑定和 manifest schema，
并在停写、冷备后执行 `apply`。`0025` 发现任何非 `EXACT` 行都会失败，不会自动修改或删除历史记录；
`0026` 为 Acceptance worker 增加独立 owner/generation/lease/attempt 字段，必须在停写窗口中迁移。

重建固定运行镜像并核对源码标签：

```bash
bash scripts/build/build_p3_images.sh
bash scripts/build/build_p4_image.sh
bash scripts/build/build_p42_images.sh
uv run platform-admin migrate
uv run platform-admin check
bash scripts/checks/check_p5.sh --phase identity
bash scripts/checks/check_p5.sh --phase all
```

统一门禁覆盖精确绑定篡改、同名端口、scatter/数组位置、显式登记、被替代登记、`0008→0009` 升级、仓库外安装、真实 PostgreSQL 和固定 checker 容器，并回归 P0–P5-R0。

回退到 `0.6.3` 前必须停止创建 R1 Deliverable 和 Acceptance，并确保没有 R1 验收在途。由于 `0.6.3` 不理解精确角色，正常回退需要恢复升级前数据库备份；仅执行 Alembic downgrade 会删除 R1 身份模式和登记选择列，不能用于保留已经形成的 R1 科学审计历史。
