# V7 参数登记

本表只记录参数边界，不记录任何密钥、Token、路径实例值或 Release Lock 内容。所有旧 `V7_*` 名称仍是
兼容输入，但只由可执行应用的 composition/bootstrap 层读取并转换为纯 Contracts context；业务层只接收
显式依赖。

| 参数组 | 所有者与解析点 | 消费者 | 默认/验证 | 发布锁与敏感性 |
| --- | --- | --- | --- | --- |
| `V7_DEPLOYMENT_MODE`、`V7_DEPLOYMENT_ID`、`V7_DEPLOYMENT_REVISION` | 各应用 composition → `DeploymentContext` | deployment header、容器标签、备份绑定与追踪 | `direct` 只校验值形状；不读取锁文件或授权启动 | 非敏感部署元数据；不是发布门禁 |
| `V7_OPERATOR_CATALOG`、`V7_CHECKER_CATALOG`、`V7_*_IMAGE` | Platform/Execution/Agent composition → `CatalogContext`，Platform/Agent/Authoring composition 读取并注入 catalog 值 | catalog/checker 校验、driver 选择 | image 必须是 sha256 URI；消费 catalog 的可执行应用要求固定文件 | 显式 artifact 输入；不能从 application 隐式 fallback |
| `V7_DATA_URL`、`V7_KNOWLEDGE_URL`、`V7_EXECUTION_URL`、`V7_PLATFORM_URL` 及对应服务 Token | 各服务 composition → `ServiceEndpoint` | HTTP clients、内部授权依赖 | URL 与最小 token 形状校验；client 使用显式 endpoint | Token 敏感，`repr`/审计输出不得回显 |
| `V7_EXECUTION_WORKDIR`、`V7_EXECUTION_CALLBACK_URL`、`V7_PARK_MANIFEST`、remote credential/CA directory | Execution composition → `ExecutionRuntimeConfig` | materialize、driver、publication、remote adapter | work root 必须绝对；callback 必须 HTTP(S)，内部字段名为 `workflow_engine_callback_url` | 目录/凭据敏感；application 不读取环境 |
| `V7_DEPLOYMENT_ID`、`V7_DEPLOYMENT_REVISION`、`V7_TEST_RUN_ID` | Execution composition → `ContainerIdentity` | Docker labels、可追溯 cleanup/verification | 文本形状校验 | 仅身份标签；不决定启动授权 |
| `V7_WORKER_HEARTBEAT_FILE` | Platform/Execution/Agent/Data/Knowledge composition → 显式 `Path` | 各 worker loop 的 `touch_worker_heartbeat(path)` | 缺失时不写 health 文件；运行循环不读取环境 | 仅存活信号；绝不能作为工作租约续期 |
| `V7_DATABASE_URL`、各服务数据库 URL | 对应 Platform/Data/Knowledge/Execution/Agent composition | Database/UoW 构造 | 由各应用的数据库 bootstrap 校验 | 连接信息敏感；不写入报告 |
| `V7_AGENT_RUNTIME_PROFILE`、`V7_MODEL_CATALOG`、`V7_AGENT_TOOL_CATALOG`、`V7_EMBEDDING_CATALOG` | Agent compositions | Agent Worker、Authoring、Admin、Backup | 目录与 digest-frozen catalog 由 composition/loader 校验 | Provider credential 仅在 composition 读取后注入 adapter，不持久化 |
| `V7_IDENTITY_MODE`、`V7_TRUSTED_PROXY_TOKEN`、`V7_MAINTENANCE_FILE` | Platform/Gateway composition | identity resolver、internal authorization、maintenance dependency | token/trusted-proxy 两种模式；proxy 必须去除伪造头 | trusted proxy secret 与 maintenance state 不可输出 |

验证入口：`runtime/toolchain/v7/bin/python source/scripts/maintenance/check_architecture_boundaries.py`。
它是源码门禁，不连接 Docker、PostgreSQL 或 P8，也不读取环境变量值。
