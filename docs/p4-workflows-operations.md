# P4.1 安装、迁移与运行

P4.1 需要 Python 3.12、uv、Docker Engine 和 PostgreSQL 17。Toil 9.5.0 与 cwltool 被锁在独立运行时文件 `runtimes/cwl/uv.lock` 中，并构建为固定镜像。平台核心不依赖 Toil 或 CWL SDK。

## 干净安装

```bash
uv sync --locked
bash scripts/build_p3_images.sh
bash scripts/build_p4_image.sh
uv run python scripts/configure_p3.py
set -a
source .p3-runtime/local/p3.env
set +a
docker compose --env-file .p3-runtime/local/p3.env -f deploy/p3.compose.yaml up -d --wait
uv run --locked platform-admin migrate
uv run --locked data-admin migrate
uv run --locked execution-admin migrate
bash scripts/run_p4.sh
```

`build_p4_image.sh` 从锁文件按哈希下载 wheel，使用摘要固定的 Python/Node 基础镜像构建 `groundwater-v7-cwl:0.3.8`，并把不可变 image ID 写入 `.p4-runtime/cwl-image.txt`。生产启动使用 image ID，不接受 API 请求中的镜像、命令或引擎 profile。

## 从 P3 升级

升级顺序固定为：

1. 停止 Platform API、Data/Execution 服务及全部 Worker；等待或按运维决定终止旧 P3 计算。
2. 安装八个 `0.3.8` 包并构建/分发固定 CWL 镜像。
3. 依次执行 `platform-admin migrate`、`data-admin migrate`、`execution-admin migrate`。
4. 分别执行三个 `check-schema`，确认 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0004_execution_work_class`。
5. 设置 `V7_CWL_IMAGE` 为构建记录中的 `sha256:` ID，设置 `V7_EXECUTION_CALLBACK_URL` 为 Execution 服务对本机 Toil 容器可达的内部地址，再启动服务和 Worker。

旧进程不得和新迁移版本混跑。启动只检查迁移版本，不自动迁移。新增迁移是追加式的：保留 P3 任务、预算、授权、Action、幂等、资产、发布、执行和恢复材料；旧授权不会自动获得工作流包或新的叶子算子许可。

## 运行目录和网络

- Platform、Data、Execution 继续使用独立数据库与角色，不读取彼此的表。
- `V7_EXECUTION_WORKDIR` 必须是 Execution API、Execution Worker 和 Docker daemon 可访问的持久本机目录。JobStore、中间节点输出和发布恢复材料都在这里。
- Toil 使用 host 网络只为访问本机 Execution 桥接端口；容器没有数据库凭据、用户令牌和 Docker socket。节点算子容器继续禁用网络。
- 当前恢复保证建立在同一主机、同一 Docker daemon 和持久工作目录上；本轮没有跨主机 HA 声明。

## 验收

```bash
bash scripts/check_p4.sh --phase workflows
```

统一入口会清除继承的 `V7_*` 配置，使用临时 PostgreSQL/服务角色和目录，构建固定 P3 与 CWL 镜像，运行静态检查、全部 pytest、13 项 P1 规则、真实 Chromium，以及仓库外 wheel/sdist 安装后的 P3 和 P4 完整闭环。P4 必测项不允许 skip。结果写入 `.p4-runtime/verification/`，结构化证据写入 `docs/verification/p4-workflows-evidence.json`。

测试数据是合成监测时间序列，不是真实地下水观测。P1 的历史 Weaver/gVisor 重型探针不在本入口重复运行；P4.1 会真实运行 Toil、cwltool、算子容器和故障恢复。

大量合成数据的固定单机性能回归使用独立入口：

```bash
bash scripts/check_p4_performance.sh --profile standard
```

该入口使用一次性数据库和服务，不连接部署实例。场景、配置和结果解释见 [工作流性能测试](p4-workflow-performance.md)。

R4.3 图压平回归使用生产运行器，所有性能节点都执行固定 30 CPU 秒负载：

```bash
bash scripts/check_p4_scheduler_r43.sh --profile quick
bash scripts/check_p4_scheduler_r43.sh --profile standard
```

这是 R4.3 的历史对照：quick 执行一次 4 节点预检，standard 执行三次 8 节点工作流；两者固定两个 Execution Worker、并发上限 4、嵌套 scatter 和每节点 30 CPU 秒。R1–R3 对照结果同样作为历史证据保留。R2 的内部 GET 长轮询每次最长 5 秒，等待期间不持有数据库事务。

R4.4 并发队列验收使用：

```bash
bash scripts/check_p4_scheduler_r44.sh --profile standard
bash scripts/check_p4_scheduler_r44_roles.sh --profile quick
bash scripts/check_p4_scheduler_r44_roles.sh --profile standard
```

standard 同时运行两条各含 8 个固定 30 CPU 秒节点的工作流。`run_p4.sh` 启动 1 个 supervisor、8 个 node 和 1 个 publication Worker；supervisor 也可领取 publication 对账，保证两个成果组同时完成时不串行阻塞。P3 单独运行仍默认一个 `all` Worker；可用 `V7_EXECUTION_WORKERS` 配置通用进程，或用空格分隔的 `V7_EXECUTION_WORKER_ROLES` 显式配置角色。

R5 chaining 对照使用：

```bash
bash scripts/check_p4_scheduler_r5.sh --profile quick
bash scripts/check_p4_scheduler_r5.sh --profile standard
```

standard 先运行 chaining-on 的真实恢复和取消资格测试，再按平衡顺序对 off/on 各运行三次 8×30 CPU 秒工作流。当前性能结果受同机仓库外负载干扰，需在独占或稳定负载窗口复测；这不改变正式 profile 固定为 `toil-single-host-v2-chaining-off`，也不会增加面向请求或部署环境的 profile 开关。

## `0.3.8` 升级与回退

从 `0.3.7` 升级前等待活动引擎和发布收敛，停止 API、服务及全部 Worker。安装 `0.3.8`、重建并记录 CWL 镜像摘要、确认三个数据库仍为 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0004_execution_work_class`，然后按原角色布局启动。R5 没有数据库迁移。

新工作流在首次接受时冻结受信 Toil profile；旧执行没有该字段时按 chaining-off 恢复，不重写 ExecutionPlan、EnginePackage、JobStore、节点账本、预算或成果。回退到 `0.3.7` 不降级数据库。正式版本未启用 chaining-on；仍应等待活动工作流收敛后再回退，避免旧进程接管带新诊断字段的活动执行。

## `0.3.7` 升级与回退

从 `0.3.6` 升级前等待活动引擎和发布收敛，停止 API、服务及全部 Worker。安装 `0.3.7` 后显式执行 `execution-admin migrate` 并确认 `0004_execution_work_class`，再按角色布局启动。迁移把发布中的记录归为 `PUBLICATION_RECONCILE`，工作流根记录归为 `WORKFLOW_SUPERVISOR`，其余记录归为 `NODE_EXECUTION`；原请求、身份、租约、预算、JobStore 和成果均保留。

回退前让活动执行收敛并停止所有进程，执行 Execution 降级到 `0003_workflow_trace`，安装 `0.3.6` 并恢复对应镜像。降级只删除领取分类和索引；不删除执行、节点、trace、预算、JobStore 或成果。不同 schema 版本的 API/Worker 不得混跑。

## `0.3.6` 升级与回退

从 `0.3.5` 升级前，等待活动 Toil leader 和发布操作收敛，再停止 V7 API、服务和全部 Worker。安装八个 `0.3.6` 包、重建固定 CWL 镜像并核对摘要；三个 schema 仍为 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0003_workflow_trace`，不执行新迁移。

新提交的完全静态工作流保存并运行原 `flat_v1` 引擎包；存在动态 scatter 的执行保存完整 `legacy_v1` 包。恢复时核对源包、计划、引擎包摘要及容器标签，不重新生成图。`0.3.5` 创建的载荷没有 `engine_package` 时继续使用原 `compiled` 文件和 JobStore。

回退到 `0.3.5` 前必须让所有 `flat_v1` 活动执行进入终态；旧 Worker 不理解单层引擎包，不能接管这些执行。回退不降级数据库，也不删除任务、预算、JobStore、节点账本或成果。

## `0.3.5` 升级与回退

从 `0.3.4` 升级前，先等待活动 Toil leader 和发布操作收敛，再停止 V7 API、服务和全部 Worker。安装八个 `0.3.5` 包、重建固定 CWL 镜像并核对摘要；显式执行三个 `check-schema`，版本仍为 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0003_workflow_trace`。R4.2 不新增迁移或表。

新提交会在父 Action 和 Execution 不可变请求中保存 ExecutionPlan。升级前创建、没有该字段的执行继续读取原 `compiled` 结果恢复；升级后创建的执行必须携带并复用原计划。回退前必须让所有带计划的活动工作流收敛，停止服务后安装 `0.3.4` 并恢复对应镜像摘要；已完成的计划只是历史请求数据，不需要删除或重写。运行中的 `0.3.5` 工作流不能交给 `0.3.4` Worker 接管。

## `0.3.4` 升级与回退

从 `0.3.3` 升级前，先等待活动 Toil leader 和正在发布的工作流收敛，再停止 V7 API、服务和全部 Worker。安装八个 `0.3.4` 包、重建固定 CWL 镜像并核对镜像摘要；显式执行三个 `check-schema`，数据库版本仍为 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0003_workflow_trace`。R4.1 不新增迁移。旧 bridge 可以向新 Execution API 提交 `engine_job_id`，但升级期间不混跑新旧 leader，以便运行证据只对应一个固定镜像。

回退同样先让活动工作流收敛并停止服务，再安装 `0.3.3`、恢复对应 CWL 镜像摘要并启动。节点、预算、trace、JobStore、工作目录和成果不参与回退，也不得删除或改写。

## `0.3.3` 升级与回退

从 `0.3.2` 升级前，先等待所有处于 `ENGINE_CREATED`、`ENGINE_RUNNING` 和 `PUBLISHING` 的工作流收敛，再停止 V7 API、服务和全部 Worker。安装八个 `0.3.3` 包、重建固定 CWL 镜像，显式执行三个 `check-schema` 并确认 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0003_workflow_trace` 后，启动服务和两个 Execution Worker。R3 不新增数据库迁移；活动的旧 CWL leader 不得交给只配置新镜像的 Worker 接管。

回退同样先让活动工作流收敛并停止服务，再安装 `0.3.2`、恢复对应 CWL 镜像摘要并启动。任务、预算、节点、JobStore、工作目录和成果不参与回退，也不得删除或重写。

## `0.3.2` 升级

从 `0.3.1` 升级时停止 V7 API、Toil leader 和全部 Worker，安装八个 `0.3.2` 包并重建固定 CWL 镜像。R2 不新增数据库迁移，仍显式执行三个 `check-schema`，确认 Platform `0004_workflows`、Data `0002_publication_groups`、Execution `0003_workflow_trace` 后，再启动服务与两个 Execution Worker。旧 bridge 进程不得与新的 Execution API 混跑。

## `0.3.1` 升级与回退

升级必须停止 V7 API 和 Worker，安装 `0.3.1` 后显式执行 `execution-admin migrate`，确认 Execution schema 为 `0003_workflow_trace`，再启动服务并依次运行 quick 和 standard。不同迁移版本的 Execution API/Worker 不得混跑。

回退前先复制 `docs/verification/p4-workflow-scheduler-diagnostics.json` 和对应报告，再停止服务，执行 Execution 迁移降级到 `0002_workflows`，安装 `0.3.0` 后启动。降级只删除 `workflow_trace_events`；执行、节点、预算、JobStore、工作目录和发布成果不得删除或改写。
