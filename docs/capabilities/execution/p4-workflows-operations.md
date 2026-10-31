# Workflow 执行运维

Workflow 执行要求 Python 3.12、锁定的 workspace 依赖、项目 rootless Docker 和独立
PostgreSQL 服务。Toil/cwltool 由 `runtimes/cwl/uv.lock` 固定并构建成不可变镜像；平台核心不
直接依赖其 SDK。

## 构建与启动

```bash
uv sync --locked
bash scripts/build/build_p3_images.sh
bash scripts/build/build_p4_image.sh
uv run --locked platform-admin migrate
uv run --locked data-admin migrate
uv run --locked execution-admin migrate
bash scripts/operations/run_p4.sh
```

生产配置必须使用构建记录中的不可变 CWL image ID。API 请求不能覆盖镜像、命令或引擎 profile。
服务启动只检查 schema，不自动迁移；不同 schema 的 API 与 Worker 不得混跑。

## 状态与网络边界

- Platform、Data 与 Execution 使用独立数据库角色，不跨库查表。
- `V7_EXECUTION_WORKDIR` 是持久工作目录，由 Execution API、Worker 和项目 Docker daemon 共享；
  JobStore、中间输出与发布恢复材料都在这里。
- Toil leader 只能访问所需内部服务；科学节点容器禁用网络，不获得数据库凭据、用户令牌或
  Docker socket。
- Workflow 在首次准入时冻结 source、ExecutionPlan、EnginePackage、运行 profile 和目录摘要；
  恢复复用冻结值，不重新发现“最新”输入。
- 当前恢复合同是单主机、单项目 Docker daemon；不声明跨主机自动 HA。

## 验收

```bash
bash scripts/checks/check_p4.sh --phase workflows
```

入口使用临时数据库、独立角色与目录，运行静态检查、pytest、分发安装、真实 Toil/cwltool、
容器故障恢复和浏览器闭环。日志、JUnit 和结构化结果写入
`${V7_P4_RUNTIME_ROOT}/verification/`，不回写源码文档。

性能与调度回归使用：

```bash
bash scripts/checks/check_p4_performance.sh --profile standard
bash scripts/checks/check_p4_scheduler_diagnostics.sh --profile standard
bash scripts/checks/check_p4_scheduler_r43.sh --profile standard
bash scripts/checks/check_p4_scheduler_r44.sh --profile standard
bash scripts/checks/check_p4_scheduler_r44_roles.sh --profile standard
bash scripts/checks/check_p4_scheduler_r5.sh --profile standard
```

这些入口均使用一次性服务或 `runtime/work/p4/` 下的本次运行材料。后续基准需要前序基准时，
只读取同一 runtime 根中的明确文件；缺失基线会失败，不会从源码报告回填。

## 更新与回退

更新前必须停止准入、等待或显式终止活动执行、完成可恢复冷备，并停止所有相关 API 与 Worker。
随后整体替换包、Catalog 和不可变镜像，显式迁移并逐项检查 schema，再运行 readiness 与真实工作流。

回退同样要求活动执行收敛和所有进程停止。不得删除或改写 Task、Action、预算、ExecutionPlan、
EnginePackage、JobStore、节点账本、trace 或已发布成果。目标版本无法理解的活动执行不能交给旧
Worker 接管；应从经验证的冷备恢复到隔离实例。
