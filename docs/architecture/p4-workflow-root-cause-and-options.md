# P4.1 工作流并行效率根因与开源方案评估

日期：2026-10-12。分析基线：V7 `0.3.0` 长节点标准性能证据。本报告只提出 P4.1 优化和后续执行后端路线，不进入 P4.2。

## 结论

当前问题不是算子慢、PostgreSQL 锁死或 Execution 完全串行，而是 **Toil 和 Platform 同时管理节点并发，加上 V7 为稳定 scatter 身份生成了较重的 CWL 作业图**。

保留 Toil，先调整适配层，风险和成本最低。短期应让 Toil 只判断 DAG 就绪，让 Platform 的节点许可和预算成为实际计算并发的唯一权威；把只负责 HTTP 提交/轮询的 CWL 桥接工具声明为小资源协调任务；消除不必要的 scatter 子工作流层；将 Toil 内部作业 ID 从权威节点身份降为诊断关联。达到这些条件后再启用 job chaining。

如果完成这些优化后，8 个固定 30 CPU 秒节点仍不能稳定达到两核有效并行度 1.8 以上，或者进程组回收警告不能消除，再进入 Argo Workflows 后端验证。Temporal、Nextflow 和 Flyte 各有成熟设计可参考，但都不适合直接替换当前受限 CWL 与 V7 权限/预算核心。

## 已确认的事实

[标准证据](../verification/p4-workflow-performance-evidence.json)记录：

- 10 个节点均运行 30.52–31.24 秒，节点负载稳定。
- 双节点场景相隔 0.224 秒启动，计算窗口有效并行度为 1.979，证明 Platform、Execution 和 Docker 能同时运行两个节点。
- 8 节点 scatter 的启动偏移为 0、11.13、39.41、57.88、85.68、100.05、134.95 和 142.53 秒；计算窗口有效并行度为 1.412/2，端到端为 1.070。
- 两个场景均只有一个 Toil 引擎代次，失败 Worker 为 0，预算全部结算，节点没有换身份或重复执行。
- Toil 日志在双节点场景记录 2 次、scatter 场景记录 17 次进程组回收警告。

这些事实排除了“算子时长差异造成尾部拖延”和“系统只能启动一个容器”。损失发生在 scatter 作业展开、桥接工具启动、节点许可、节点完成回传及下一个桥接作业调度之间。

## 根因分解

### 1. 两套并发控制重复限流

Platform 已经通过任务 `max_in_flight`、节点许可和预算账本控制真实计算并发。Toil 引擎又固定使用 `--maxCores 2`，而生成的桥接 CommandLineTool 没有 `ResourceRequirement`，因此按 Toil 默认值占用 1 核。

桥接工具本身不执行科学计算。它向 Execution 提交稳定节点，然后每秒查询一次状态；真正的计算发生在另一个受 1 CPU 限制的 Docker 容器中。于是一个外部计算节点同时占用：

1. Platform 的一个在途名额；
2. Toil 的一个完整 CPU 调度名额；
3. Docker 的一个实际 CPU 配额。

任务并发上限为 4 时，Toil 仍把并发压到 2。CWL 允许使用小数 `coresMin` 表示调度资源；Toil 的 single-machine 后端也支持小数核心。因此桥接工具应按协调开销申请固定小资源，实际计算容量继续由 Platform 管理。[CWL ResourceRequirement](https://www.commonwl.org/v1.2/CommandLineTool.html#ResourceRequirement)、[Toil 资源选项](https://toil.readthedocs.io/en/master/running/cliOptions.html#resource-options)

### 2. 一个 scatter 叶子被编译成多层作业图

当前性能 scatter 包本身包含条件子工作流。V7 编译后形成五份 CWL 文档：

1. 受信桥接 CommandLineTool；
2. 调用桥接工具的条件工作流；
3. 给数组元素写入 ordinal 的 ExpressionTool；
4. 每个 scatter 实例使用的包装工作流；
5. 顶层 scatter 工作流。

所以每个实际节点不是一个可运行工具，而是“标号表达式 → scatter 包装工作流 → 条件子工作流 → 桥接工具”。Toil 对 scatter 会建立展开子作业和 gather 作业；嵌套工作流继续增加图遍历、进程和 JobStore 更新。[Toil CWL 架构](https://toil.readthedocs.io/en/releases-9.5.0/appendices/architecture.html#toil-support-for-common-workflow-language)、[CWL scatter 语义](https://www.commonwl.org/v1.2/Workflow.html#WorkflowStep)

稳定 ordinal 是必要的，因为相同输入值的不同实例必须拥有不同平台身份。额外的包装工作流不是必然要求：可以让外层 scatter 直接运行桥接工具，在同一 step 的 `valueFrom` 中解开带 ordinal 的 envelope 并构造 `v7_context`。该改造必须先通过三种 scatter、嵌套 scatter、重复值、条件和恢复测试。

### 3. 为读取 Toil 作业 ID，关闭了 job chaining

桥接器沿父进程树寻找 `_toil_worker` 参数来取得 JobStore ID。为保证一项桥接调用对应一个 Worker 命令，引擎固定设置 `--disableChaining true`。

Toil 官方架构说明，job chaining 会让资源相容的后继作业直接在当前 Worker 中执行，减少 Leader 再调度和 JobStore 往返；V7 正好关闭了这项优化。[Toil job chaining](https://toil.readthedocs.io/en/releases-9.5.0/appendices/architecture.html#job-chaining)

平台节点身份实际上已经由 `workflow_execution_id + call_path + scatter_indices` 确定，不依赖 Toil JobStore ID。应把 Toil ID 改成可选诊断字段或 Worker 链关联，不能再让它决定平台身份。完成该解耦后才能 A/B 测试 chaining；不能直接删掉开关，否则旧 Worker/重放关联可能失真。

### 4. 单 Execution Worker 放大了状态推进延迟

当前基准只启动一个 Execution Worker。Worker 每次领取一行、完成一个状态转换、把该行延后 0.2 秒再释放；一个节点从接受到启动需要准备输入、许可、创建和启动多个转换。`SKIP LOCKED`、租约代次和每执行目录文件锁已经允许多个 Worker 安全竞争，因此部署多个 Worker 不需要改变领域状态机。

这不是双节点场景的硬瓶颈，因为两个节点可以在 0.224 秒内相继启动；但 scatter 同时出现更多父子记录时，一个 Worker 会增加就绪节点的排队时间。需要以 1、2、4 个 Worker 做同负载对照，不能仅凭代码推断收益。

### 5. 子进程回收异常会增加尾部和抖动

Toil 9.5.0 的 single-machine 后端在关闭时会杀死子进程组并等待最多 5 秒；进程组仍存在时输出当前警告。V7 的 CWL 容器没有使用 Docker `--init`，PID 1 是 Python 引擎进程。17 次警告与复杂 scatter 图同时出现，是明确的运行时健康问题，但现有证据还不能证明每条警告都串行增加 5 秒。

应先给引擎容器增加最小 init/reaper，并保存带时间戳的 Toil 调度事件，验证警告来自 Node 表达式进程、Toil Worker 还是取消/关闭路径。不能通过过滤日志把问题隐藏。

## 成熟开源方案对比

| 方案 | 可借鉴能力 | 与 V7 的匹配 | 判断 |
| --- | --- | --- | --- |
| **Toil 9.5** | 原生 CWL、JobStore 恢复、多批处理后端、资源调度、job chaining 和文件缓存 | 已集成并通过故障恢复；当前问题集中在 V7 适配和单机参数 | **保留并先优化** |
| **Argo Workflows** | Kubernetes 原生 DAG/Steps、每任务容器、工作流/模板并行上限、数据库信号量、重试和取消；CNCF 毕业项目 | 并行与可观测性强，但需要 Kubernetes；Argo 使用自己的 Workflow CRD，V7 需要从冻结包编译到 Argo DAG，并继续外接 Platform 许可 | **作为第二执行后端验证**，[官方概览](https://argo-workflows.readthedocs.io/en/latest/)、[并发与信号量](https://argo-workflows.readthedocs.io/en/latest/synchronization/) |
| **Flyte** | 类型化任务/工作流、不可变注册、Kubernetes Pod 执行、队列/状态服务和多集群路由 | 科学平台理念接近，但不是 CWL 执行器；引入后会与现有 Data、Platform 和工作流包注册重叠 | **参考类型和控制/执行分层，不直接迁移**，[Flyte 后端架构](https://github.com/flyteorg/flyte/blob/main/docs/BACKEND_README.md) |
| **Temporal** | 持久事件历史、Activity、Task Queue、心跳、重试和按 Worker slot 限流 | 适合业务长事务，但不提供 CWL/scatter 科学语义或容器资源调度；V7 P2 已实现相近的幂等、租约和恢复核心 | **不替换 P2/P4；参考 Worker slot 和心跳指标**，[Worker 性能](https://docs.temporal.io/develop/worker-performance/) |
| **Nextflow** | 数据流执行、隔离工作目录、任务缓存和 `-resume`、多种 executor | 科学计算成熟，但使用自己的 DSL 和进程模型，允许的表达能力远大于 V7 受限目录；无法直接保留当前 CWL 外部契约 | **参考缓存/恢复，不作为当前后端**，[执行与恢复](https://www.nextflow.io/docs/latest/cli.html) |
| **GA4GH WES/TES** | WES 标准化工作流提交/查询/取消；TES 标准化容器任务、输入输出和资源 | 它们是接口标准，不是调度器；WES 明确把输入输出凭据等授权细节留给实现 | **P4.3 对外/远程端口采用其语义**，[WES](https://ga4gh.github.io/workflow-execution-service-schemas/docs/)、[TES](https://ga4gh.github.io/task-execution-schemas/) |

Argo 官方把每个步骤建模为容器并提供 DAG 最大并行、模板/工作流信号量和数据库协调。这个设计验证了 V7 应将“图上可运行”和“全局资源许可”分开，但直接换 Argo 会带来 Kubernetes、CRD 状态、制品存储和身份映射四套新运维面。Toil 优化尚未做完时切换引擎，无法判断收益来自更换引擎还是修正了错误的桥接资源模型。

Temporal 的 Worker slot 模型也值得采用：协调任务和实际计算任务使用不同 slot 类型，并根据 CPU/内存分配容量。V7 当前把桥接轮询和实际计算都折算成 1 个 Toil core，正是应该拆开的部分。[Temporal Worker slots](https://docs.temporal.io/develop/worker-performance/#task-slots)

## 建议的目标结构

```text
Platform（唯一计算准入权威）
  Task / Grant / Budget / Node ledger
                 │ permit / observation
                 ▼
Workflow Engine Adapter（只负责 DAG 就绪和依赖）
  Toil JobStore + CWL runner
  小资源 bridge slots，不持有实际计算额度
                 │ stable Platform Node ID
                 ▼
Execution Worker Pool（负责尝试与容器事实）
  claim / create / start / inspect / collect
                 │ verified outputs
                 ▼
Data（原子发布）
```

必须保留两个独立标识：

- `node_id`：由 Platform 定义且跨恢复稳定，是授权、预算、幂等和成果来源的权威键；
- `engine_binding`：Toil JobStore ID、Worker 链 ID 或后续 Argo Pod UID，只用于诊断和取消定位，可以随引擎后端变化。

## 分轮解决方案

### R1：测量和低风险配置探针

不改状态机，增加以下时间点：Toil job ready、bridge 启动、Execution 接受、许可取得、容器创建、容器启动、容器结束、bridge 返回、gather 完成和发布完成。

使用同一组 8 × 30 CPU 秒长节点依次运行：

1. 当前复杂条件子流程 scatter；
2. 直接 scatter；
3. 1、2、4 个 Execution Worker；
4. CWL 引擎有无 Docker `--init`。

这轮回答图膨胀、Worker 数量和进程回收各自贡献多少，不改变生产行为。

### R2：统一资源准入

给受信桥接 CommandLineTool 固定加入资源声明，首个候选为 `coresMin/coresMax = 0.25`、`ramMin/ramMax = 128 MiB`。引擎协调槽先取 `min(task.max_in_flight, 8)`，`maxJobs` 随协调槽设置；Platform 的 `max_in_flight` 继续决定实际容器数。资源值属于登记实现，不允许用户覆盖，128 MiB 也必须经过实测峰值校正。

把引擎 `maxCores`、`maxJobs` 和 Worker 副本数移入受控运行配置，配置摘要写入执行记录。禁止 API 请求直接指定这些值。

通过门槛：任务并发为 4 时，8 节点计算窗口有效并行度至少 3.2/4；预算、取消、授权到期和旧 Worker 测试不退化。

### R3：压平编译图并恢复 chaining

1. 保留 ordinal envelope，但让 scatter step 直接调用叶子桥接工具；删除每实例包装子工作流。
2. 将 Toil Job ID 从权威身份中移除，保存为可空 `engine_binding`。
3. 在稳定节点身份、重复值、三种 scatter、嵌套流程和引擎重启全部通过后，移除 `--disableChaining true` 做 A/B。
4. 给 CWL 引擎容器增加 init/reaper，要求进程组回收警告为 0。

通过门槛：两核配置的有效并行度至少 1.8/2；节点启动间隔 p95 不超过 3 秒；Toil 无失败 Worker、无回收警告；恢复后不重复计算、发布或扣费。

### R4：替代引擎决策门

仅在 R2、R3 完成后仍达不到门槛时实施 Argo 探针：

- 同一冻结工作流编译为 Argo DAG；
- Pod 标签携带稳定 `node_id`，Pod UID 仅作为 `engine_binding`；
- 每个 Pod 启动前仍调用 Platform permit；
- Argo `parallelism` 只限制控制器负载，Platform 继续掌握项目预算；
- 复用相同 8 × 30 秒负载、响应丢失、取消和旧控制器测试。

Argo 只有在性能、恢复和运维成本三项都优于优化后的 Toil 时才进入 P4.3 后端目录。Flyte、Temporal 和 Nextflow 不进入这轮选型。

## 不建议的处理

- 只把 `--maxCores` 从 2 调大：会缓解上限，但仍保留重复准入、重图和 Worker ID 耦合。
- 删除 Platform 节点许可：会破坏任务修订、授权到期、预算和取消竞争的权威语义。
- 打开 Toil 自动重试：V7 要求已开始且中断的科学计算不能被引擎偷偷重算。
- 过滤进程组警告：它会掩盖真实的子进程生命周期问题。
- 立即换成 Argo/Flyte/Temporal：迁移会同时改变调度、状态、部署和身份，无法隔离根因。

## 推荐决定

下一开发轮应实施 **R1 测量与探针**，结束后单独报告，不同时实施 R2。R1 不改变正式接口和状态机，也不进入 P4.2。根据 R1 数据确定 R2 的桥接资源值和 Worker 数量，再进行一次开发、测试和验收。
