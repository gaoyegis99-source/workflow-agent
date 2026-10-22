# P8.2 维护窗口升级与回滚

P8.2 使用单机维护窗口把 V7 `0.15.2` 升级到 `0.15.3`。当前活动 Release 身份以[机器可读运行指针](../verification/p82-current-release.json)为准；本文记录该版本的升级路径。升级不会运行不同版本的 API、Worker 或 Catalog；gateway 在完整验证和 Worker 接管前始终保持维护状态。

## 发布物与兼容范围

`0.15.3` Release Lock 使用 schema v3，固定 API 契约、四个数据库 schema、备份格式、最低运行要求、当前目录和镜像，以及唯一允许恢复的 N-1 发布 `0.15.2`。发布目录不可覆盖：

```text
.p8-runtime/releases/0.15.3-<release-digest>/
```

新 Action 只使用当前目录。`0.15.2` 已冻结执行继续使用原 EnginePackage、Catalog、实现和镜像。终态历史对象不重写。

内部服务请求携带 `X-V7-Release-Digest`。生产 API 遇到错误摘要或普通内部请求缺少摘要时 readiness/请求失败。唯一兼容例外是 `/internal/workflows/` 下由 `0.15.2` 冻结 CWL bridge 发出的无摘要请求；这些路由仍必须通过执行专属高熵 bridge token、父 Action 和 epoch 校验。显式提供错误摘要始终拒绝。

## 操作顺序

```bash
bash scripts/p8-upgrade.sh plan SOURCE_ENV TARGET_RELEASE
bash scripts/p8-upgrade.sh prepare SOURCE_ENV TARGET_RELEASE OPERATION_DIR
bash scripts/p8-upgrade.sh apply OPERATION_DIR
bash scripts/p8-upgrade.sh verify OPERATION_DIR
bash scripts/p8-upgrade.sh commit OPERATION_DIR
```

状态按以下顺序只追加记录：

```text
PLANNED → BARRIER_ACTIVE → PARKED → BACKED_UP
→ TARGET_INSTALLED → TARGET_VERIFIED → COMMITTED
```

`prepare` 会打开维护屏障、停止 Agent/Authoring 生产者、等待模型调用和原子发布收敛、停车 Toil、保存 `park-manifest.json`，再生成并校验 schema v2 冷备份。活动本地科学容器最多等待 240 秒；未交接输出时停车失败并恢复源服务，不强杀后继续备份。

`apply` 只启动目标 PostgreSQL、迁移检查、四个 API 和 gateway。`verify` 检查四数据库、四棵状态目录、Release Lock、Catalog、Profile、Model Catalog、旧历史对象及 park manifest。`commit` 启动 Worker，将目标环境原子安装为 canonical env，最后解除维护屏障。

## 活动工作流恢复

停车清单绑定源/目标 Release digest、父 Action/Execution、engine epoch、EnginePackage、JobStore 绝对路径和关键文件摘要。恢复时：

- 使用原父 Action、节点 Action、Execution、JobStore 和成果身份；
- 维护停车可以创建新 epoch，但不重新编译或重新生成节点；
- 已开始且没有完整结果的科学节点不自动重算；
- 远程 Execution 按原身份查询，404、断线和响应丢失保持 `UNKNOWN`；
- JobStore 缺失、摘要错误、镜像缺失或身份冲突时保持不可运行状态。

同一逻辑部署不得在两个 Compose project 中并发接管同一活动 JobStore。第二个 deployment 遇到稳定容器名/标签冲突必须停止，而不是复用别的 deployment 的容器。

## 失败回滚

`COMMITTED` 之前的命令失败进入：

```text
ROLLING_BACK → ROLLED_BACK
```

回滚停止目标进程，校验升级前备份，恢复四数据库、状态目录、源 Release descriptor 和镜像，然后启动源 API/Worker。远程活动继续按原身份对账。

也可在提交前显式执行：

```bash
bash scripts/p8-upgrade.sh rollback OPERATION_DIR
```

`COMMITTED` 是不可逆边界。提交后禁止直接自动降级；必须重新停车、备份并执行独立兼容回滚计划。canonical env 只有在状态已经写为 `COMMITTED` 后才原子切换，切换或开放 gateway 失败时维护屏障保持启用。

## 核验

```bash
bash scripts/check_p8.sh --phase upgrade
bash scripts/check_p7.sh --phase compatibility
bash scripts/check_authoring.sh --phase release-lifecycle
uv run --locked python scripts/verify_distribution.py
```

P8.2 不修改业务 schema：Platform `0023`、Data `0004`、Execution `0006`、Knowledge `0002`。
