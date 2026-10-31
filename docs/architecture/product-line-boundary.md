# V7 产品线边界章程

## 定位

V7 是通用企业平台产品线：它拥有多项目/成员角色、任务与工作流治理、Agent、Execution、独立的 Data/Knowledge 服务边界，以及 Direct Compose 部署能力。P9 的通用工作流后端在这个边界内使用 Temporal 处理计时、等待、重试和编排恢复；V7 账本仍是权限、预算、Action 和业务结果的唯一权威。P9 不建设新的知识库、业务数据库或数据目录。它不承载、更不替换 V6 的地下水产品运行态、固定案例或受控试点链路。

V7 不能把 V6 的源码、Python 包、Docker socket、卷、运行目录、数据库或任何未声明数据通道作为运行依赖。V6 到 V7 只能通过 `v6-light-bundle-v1` 的受支持声明式子集交换：导入时重新审核精确工具、权限和凭据绑定，绝不导入 journal、SDK checkpoint、Python callable 或共享可写状态。若将来需要其他跨产品交换，必须先定义版本化导入/导出契约和所有权，而不是增加在线调用。

V7 的语义发现和协作能力只提供可复核建议。语义命中不用于授权、预算、验收、执行参数或
事实判定，也不构成 V6 运行态替代或跨项目共享通道。

## 状态与依赖所有权

- 可写 V7 状态只属于项目根 `runtime/`；P8 的 Direct Compose 契约为 `runtime/p8/`，工作产物、隔离区、工具链和 Docker 数据各有独立项目内根。
- 所有 Docker 调用只可通过 V7 的 rootless socket 和 `bin/docker`。宿主 socket、V6 socket、全局 context/TLS 覆盖、共享可写卷和越界链接均被拒绝。
- `apps/`、`packages/`、`runtimes/`、`deploy/`、`scripts/` 是活动源码/声明；P8 目录、Docker 数据和服务密钥永不进入 Git。
- P9 Temporal Service、其 PostgreSQL 与 Worker 是单机恢复 profile 的独立运行组成；它们不共享 V6 状态，且不承诺多机高可用。Worker 经私有治理 HTTP 契约访问既有 V7 账本，不能直接持有业务数据库连接。
- 历史 Release Lock、发布指针、验证材料和候选制品不保留在源码树中，也不能充当新 Direct Compose 实例的配置输入或启动授权。

## 运行态退役

源码树不为历史材料设置例外。需要调查旧证据时，应在工作区外部制品存储中按明确身份读取，
不得复制回当前检索边界。

`runtime/p8/` 的旧内容只能使用 `bin/ops runtime retire-p8` 退役：先盘点，再验证项目外冷备、恢复演练与外部密钥托管回执；然后隔离非敏感材料、校验，最后按固定候选执行显式删除。工具不创建新实例，不使用宿主 Docker，不全局 prune，也不删除 Docker 镜像。完成后 V7 是“源码与 Direct Compose 配置器就绪、无活动实例”的状态。

## 自动执行

`scripts/maintenance/check_workspace_dependencies.py`、项目卫生检查和布局检查是本章程的活动门禁，
已接入 `bin/ops check fast`。它们拒绝兄弟项目路径、宿主 Docker socket、共享资源声明、
越界链接，以及被 Git 跟踪的历史/候选材料。
