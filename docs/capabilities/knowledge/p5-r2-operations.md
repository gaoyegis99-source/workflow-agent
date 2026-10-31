# P5-R2 / 0.7.1 安装、升级与运行

升级前等待活动验收和检查执行收敛，停止 Platform API、Platform Worker、Execution API 与 Execution Worker。安装九个 `0.7.1` 包，显式把 Platform 数据库从 `0009_output_identity` 升到 `0010_checker_runs`。Data、Execution、Knowledge schema 分别保持 `0004_document_assets`、`0006_remote_reliability`、`0002_lifecycle_outbox`。启动不自动迁移。

构建并锁定运行环境：

```bash
bash scripts/build/build_p3_images.sh
bash scripts/build/build_p5_checker_images.sh
```

第二条命令生成 `${V7_P5_RUNTIME_ROOT}/checker-catalog.json`，其中记录通用 checker 与 GDAL checker 的不可变镜像 ID和 capability catalogue 摘要。生产进程必须统一设置：

```text
V7_CHECKER_IMAGE=<catalog.images.CHECKER>
V7_GDAL_CHECKER_IMAGE=<catalog.images.GDAL_CHECKER>
V7_CHECKER_CATALOG=/absolute/path/${V7_P5_RUNTIME_ROOT}/checker-catalog.json
```

Platform 和 Execution 启动时分别重算并核对目录；目录内容、源码能力定义或运行镜像任一不一致时，服务不能就绪。Execution Worker 还会在每次启动容器前核对 CoveragePlan 中冻结的镜像身份。

预算先为本次计划的 checker 数建立 envelope，不占计算并发槽。依赖满足后，一个 run 取得一个并发槽并创建稳定 Action；确认执行后才计入消耗。暂停阻止新 run，取消、依赖失败、许可拒绝或计划失效会释放未启动额度。计划失效时，已经准入的 Action 先请求取消并按真实执行事实结算，不能直接遗忘或提前释放；通信未知保留原 Action、Execution 和占用，不换身份重提。

ERROR 重试必须携带 `retry_of`。新验收只有在计划、来源、分析登记及目录摘要完全相同时才复用已有 PASS；仅重新创建 ERROR 能力及其未运行下游。累计实际启动次数仍受 `max_check_executions` 限制。

验收命令：

```bash
uv run --locked pytest -q tests/knowledge/checkers
bash scripts/checks/check_p5.sh --phase capabilities
bash scripts/checks/check_p5.sh --phase all
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`0.7.1→0.7.0` 仅允许数据库中没有 R2 Acceptance 时直接回退。有组合检查历史时必须恢复升级前数据库备份，不能删除 checker run 或科学检查记录。
