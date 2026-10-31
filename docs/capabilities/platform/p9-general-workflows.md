# P9 通用工作流契约

`platform_temporal_workflows` 定义 `v7-general-workflow-v1`。Studio、API、Worker 和发布流程都必须使用同一份冻结 JSON，而不是序列化 Python 闭包。

## 定义内容

- `GeneralWorkflowSpec`：workflow ID、固定版本、发布摘要、精确 `PluginManifest` 与节点树。
- `PluginManifest`：包 ID/version/digest、输入输出 Schema、权限、凭据引用名、超时、最大重试数与隔离执行模式。它不携带密钥或可执行 Python。
- `WorkflowInvocation`：run ID、输入、触发源和 `GovernanceFence`。fence 固定 project/task/actor、授权修订、预算修订和发布摘要。
- `ActionCommand`：稳定的 action/idempotency identity；Activity 重试、插件查询和账本对账必须复用它。

输入、输出、日志事件与 OTel 属性拒绝 credential-shaped 字段。插件在受审核的进程、容器或远程宿主中运行，并只收到调用上下文；它们不能直接写平台内部状态。

## 节点行为

| 节点 | Workflow 责任 | Activity/账本责任 |
| --- | --- | --- |
| Agent / HTTP / MCP / 容器 | 调度一个版本化插件 Activity；遇到未知结果等待对账信号 | 复查 fence、创建/读取 Action、调用或查询插件、记录结果 |
| 人工审批 | 持久化等待 `approve` 信号 | 记录请求/决定事实，不轮询模型 |
| 定时等待 | 使用 Temporal timer | 无外部调用 |
| 条件 / 并行 / 有界循环 | 只在冻结 JSON 上作确定性控制流 | 每个实际副作用仍走独立 Action |
| 子流程 | 启动冻结版本的 Temporal child workflow | 子流程拥有自己的 run/action identity |

已发布工作流作为工具时使用子流程节点，而非在 Agent 进程内直接调用实现。
Agent 节点的 `max_turns` 同时写入冻结的 `ActionCommand` 并参与其身份摘要；
只有 Agent 插件可以收到这一上限，其他插件携带该字段会被 Activity 拒绝。

## 私有治理 HTTP 契约

生产 Worker 可使用 `HttpGovernanceLedger`。平台内部端点是：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/internal/temporal/v1/actions/{action_id}` | 读取原 Action/结果，用于重试与对账 |
| `POST` | `/internal/temporal/v1/actions/authorize` | 原子复查 fence，并创建或重放 Action 预留 |
| `POST` | `/internal/temporal/v1/actions/dispatch` | 标记原 Action 已跨越外部边界 |
| `POST` | `/internal/temporal/v1/actions/outcome` | 写入确认结果并结算/释放原预留 |
| `POST` | `/internal/temporal/v1/actions/unknown` | 标记结果不明，禁止盲目重发 |
| `POST` | `/internal/temporal/v1/events` | 写入审批/恢复/流程生命周期事实和 outbox |

所有写入要求内部服务身份与幂等性检查；响应必须返回冻结 command、state、reservation state 和可选 outcome。`dispatch` 还返回唯一的 `invoke` 布尔值：只有获得 `true` 的那一次 Activity delivery 可以跨越外部边界；其余 delivery 只能按原 `action_id` 对账。平台 API 不应将这些端点暴露到 gateway。

取消信号只携带固定的 `CANCEL_REQUESTED` 代码，因为 Temporal signal payload 会进入
历史；经认证的 Platform API 另行把操作者说明写入受治理的审计事实。不要把原始
取消理由、提示词或凭据写入 Temporal payload、通用事件或追踪属性。

`platform_temporal_workflows.create_governance_router` 已实现这套 ASGI
线协议，并以常量时间校验服务令牌。Platform 组合层只在私有服务网络挂载
该路由，并向它注入现有事务性 V7 action/budget/outbox 账本的适配器；Worker
只持有 HTTP 契约，绝不持有数据库连接。该路由不是第二套账本。

Studio 应以这个契约展示节点状态、审批、失败原因、对账和恢复。它不从 Temporal 历史或日志推断治理结果；账本是可见业务状态的权威来源。

## 触发与信号的 outbox 契约

`TemporalDispatchCommand` 是 Platform 账本写入既有 outbox 的不可变、无凭据命令。
它涵盖手动/API/Webhook 启动、Schedule 创建、审批、对账和取消；`dispatch_id` 由 kind
和冻结 payload 推导，任何修改都会被拒绝。取消命令只含 run identity，操作者原始说明
只能留在受治理审计记录中，不能进入 Temporal history。

`TemporalOutboxDispatcher` 是 outbox Worker 的窄适配器：它重放同一个 command 到
`TemporalTriggerClient`，但不自行标记 outbox 完成、重试或释放预算。Platform 的事务
服务必须先完成授权、版本冻结、run/审批事实和 outbox 写入；只有投递成功后才由既有
lease/outbox 机制确认完成。这样 API 请求中断不会丢失或改写一个 Temporal 启动/信号。

公共 `POST /runs/manual`、`POST /runs/api` 和 `POST /schedules` 由
`create_trigger_router` 提供统一边界，并强制要求可移植的 `Idempotency-Key` 请求头。
Platform service 用它在同一事务内重放原 run 与原 outbox command；HTTP 重试不能分配
第二个 run。Webhook 不由泛化路由解析 payload，而是将原始字节和 headers 交给
provider-aware service 验签并使用 provider delivery identity 去重。Actor identity 来自
既有认证层，绝不接受请求体中的 actor 字段。

为了在不接入业务数据库的开发环境验证这条链，包提供
`SQLiteDevelopmentWorkflowPlatform`：它在一个 SQLite transaction 中保存冻结
`WorkflowInvocation`、安全 audit fact 和 `TemporalDispatchCommand`，并以 lease/generation
提供可重启的本地 outbox。该对象只用于单机开发、演示和故障契约测试；生产 Platform
必须将相同 command 结构写入已有 transaction/action/budget/outbox 账本，不能新建第二套
业务权威状态。

## 受隔离的进程插件宿主

`ProcessPluginHost` 是首个可直接使用的执行宿主：部署启动时为精确
`PluginManifest` 注册一个绝对路径的固定命令。每次调用在新进程中以
`v7-process-plugin-v1` JSON stdin/stdout 协议运行；workflow 输入不会拼接到
命令行，子进程不会继承 Worker 环境、数据库连接或账本对象。子进程只得到固定的
非敏感环境变量，以及（仅当 manifest 声明了 `credential_ref` 时）一个受控凭据文件
路径。

插件必须用原 `action_id` / `idempotency_key` 调用下游系统，并返回单个
`NodeOutcome` JSON。退出异常、超时、协议错误或结果不明都被保守地转换为
`RECONCILING`，由同一 action identity 的对账调用处理，不会自动以新身份重发。
容器、远程 HTTP 和 MCP 宿主可实现同一 `PluginExecutionPort`；它们同样只能由
Worker 启动配置注册，不能从模型输出中选择镜像、命令或模块。

`RemotePluginHost` 是对应的远程适配器：它把 HTTP、MCP gateway 或既有
容器/CWL/Toil 执行服务作为固定 HTTPS endpoint。每个精确 manifest 固定 invoke 与
reconcile URL；请求携带稳定 `action_id` 和 `idempotency_key`，而不是由模型指定 URL。
仅本机开发测试允许 loopback HTTP，其他 endpoint 必须 HTTPS。若有凭据，Worker 只从
受控文件读取并在请求头使用，绝不写入 Temporal payload、账本或通用日志。任何网络或
协议不确定性都进入对账状态。

## 观测边界

`platform_temporal_workflows.observability` 在 trigger/outbox delivery、受治理
Activity 和 process/remote plugin host 建立 OpenTelemetry span。属性白名单只包含 run、
action、node、workflow digest、plugin identity/version/mode 和 dispatch identity；没有用于
添加 workflow input、提示词、URL、HTTP headers、凭据、原始 plugin output 或异常消息的
接口。缺少或故障的 tracer 是 no-op，不能影响已治理的执行或恢复。Studio/API 应读取账本
事实展示节点、审批、失败和对账，不从 trace 或 Temporal history 推断业务结果。

## V6 导入边界

`import_v6_bundle` 是无副作用的发布前转换器。它只接受 V6 的工具、审批、条件
和并行声明，并要求目标提供完全相同摘要的已审核 `PluginManifest`、所需权限和
凭据引用绑定。V6 的普通工具按目标插件类型转换为 HTTP、MCP 或容器节点；需要
审批的 V6 工具在前方插入明确的 V7 审批节点。

导入结果只返回经过校验的 V7 定义、固定依赖元数据及必需文件摘要。调用方必须把
文件绑定到受控目标位置，不能让模型选择任意路径。V6 Agent 驱动、子流程、运行
journal、SDK checkpoint、Python callable 和工具包代码会明确拒绝，不会被伪装成
跨框架热迁移。
