# V7 项目生命周期与工作树边界

V7 是独立维护的 P8 企业平台工作树。`apps/`、`packages/`、`runtimes/`、`deploy/`、
`scripts/`、`tests/`、`benchmarks/`、`evaluations/`、`examples/`、`experiments/`、锁文件和
`docs/`（包括 `docs/verification/`）属于可审查 Git 基线。`experiments/p1/` 与
`runtimes/cwl/` 是当前验收入口的依赖，不能按历史材料清理。卫生检查同时要求 P1、P4、P5、P7
入口脚本、P1 源码和 CWL 运行时声明仍为项目内普通文件。

`.p8-runtime/` 是本机 P8 发布、实例、冷备与运行日志根目录，不进入 Git。`.venv/`、任何嵌套
`.venv/`、缓存、浏览器产物、密钥配置和生成物同样不进入 Git，均可由锁文件或发布流程重建。

当前活动发布只能通过
[`docs/verification/p82-current-release.json`](../verification/p82-current-release.json) 识别；不得根据
目录名称推断。当前指针声明的实例和发布目录不得移动或删除。

运行态归档保存在工作区外层的 `maintenance/v7-runtime-archive/<日期>/`。归档先生成清单和哈希，
复制后重新校验，且必须有独立恢复演练证据。归档工具只处理停止实例的可读元数据、发布物、备份清单和
日志；它不复制 Docker 卷、密钥或活动实例。没有通过恢复预检的候选必须保留原位。

执行以下命令验证 Git 基线边界：

```bash
uv run --locked python scripts/check_project_hygiene.py
uv run --locked python scripts/check_workspace_dependencies.py
```

运行态归档工具分为三个阶段，默认不修改任何文件：

```bash
uv run --locked python scripts/archive_p8_runtime.py plan
uv run --locked python scripts/archive_p8_runtime.py preflight \
  --rollback-instance <stopped-instance> \
  --rollback-release <verified-release> \
  --cold-backup backups/<cold-backup> \
  --restore-evidence /absolute/path/to/restore-evidence.json
uv run --locked python scripts/archive_p8_runtime.py archive --execute \
  --rollback-instance <stopped-instance> \
  --rollback-release <verified-release> \
  --cold-backup backups/<cold-backup> \
  --restore-evidence /absolute/path/to/restore-evidence.json
uv run --locked python scripts/archive_p8_runtime.py purge --execute \
  --archive-root ../maintenance/v7-runtime-archive/<日期>
```

恢复证据必须是独立生成的 JSON，且包含 schema
`v7-runtime-restore-evidence.v1`、`status: PASSED`、选定的实例与发布名称。归档仅复制过期
发布物、运行日志、指定冷备和已脱敏的实例元数据；源目录不会被自动删除。独立的 `purge` 会重新
校验归档和源端哈希，且只会删除旧发布目录和运行日志。它拒绝删除所有实例目录、冷备、Docker 卷、
密钥，以及活动或回滚发布；实例退役仍须遵循独立的 P8 运维流程。
