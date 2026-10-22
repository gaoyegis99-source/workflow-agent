# P7.1b / 0.11.1 部署与运行

本轮没有数据库迁移。Platform 保持 `0018_agent_acceptance_identity`，Data 保持 `0004_document_assets`，Execution 保持 `0006_remote_reliability`，Knowledge 保持 `0002_lifecycle_outbox`。

## 构建和门禁

```bash
uv sync --locked
bash scripts/build_p3_images.sh
bash scripts/build_p5_checker_images.sh
bash scripts/build_p71a_image.sh
bash scripts/check_p7.sh --phase tabular-acceptance
bash scripts/check_p5.sh --phase all
bash scripts/check_p7.sh --phase compatibility
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`build_p3_images.sh` 和 `build_p5_checker_images.sh` 从根 `pyproject.toml` 读取当前版本，不再把 checker wheel 固定为历史 `0.7.2`。普通 checker 镜像必须无法导入 `platform_tabular_operators`。生产部署使用同一次发行生成的 Operator、Checker 和 Tool Catalog 以及 release lock；Checker Catalog 必须指向本轮两个不可变 checker 镜像。

## 升级

```text
等待所有 Acceptance 和 checker Action 收敛
→ 停止 Platform、Agent 和 Execution Worker
→ 安装 release lock 中全部 0.11.1 wheel
→ 重建普通 checker、GDAL checker 和 tabular 镜像
→ 重新生成并核对三个 Catalog 和 release lock
→ 核对四个数据库 schema（不迁移）
→ 启动服务和 Worker
→ 运行 tabular-acceptance、P5 all 和 compatibility 门禁
```

新旧 checker 镜像不能混跑。既有 Acceptance、报告和成果不重写；新的 CoveragePlan 冻结本轮 capability、契约及镜像摘要。

## 回退

先让使用 P7.1b 能力的 Acceptance 和 checker Action 进入终态，再整体恢复 `0.11.0` 的 wheel、Catalog、镜像和 release lock。没有数据库降级。`0.11.0` 可以读取历史报告，但不能推进冻结了 P7.1b 新能力的在途验收。
