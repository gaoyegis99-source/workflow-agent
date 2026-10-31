# V7 维护所有权边界

| 区域 | 维护责任 | 不应承担的状态 |
| --- | --- | --- |
| `apps/`、`packages/`、`runtimes/` | 平台、Data、Execution、Knowledge、Agent 与 Operator 运行代码 | Docker 卷、真实实例数据 |
| `deploy/platform`、`execution`、`spatial`、`knowledge`、`agent`、`operators`、`production` | 对应能力的声明式部署资产 | 其他项目的 Compose、网络或 socket |
| `scripts/` | 可复核的构建、检查、配置、运维与发布命令 | 业务数据、密钥、临时虚拟环境 |
| `tests/{platform,execution,spatial,knowledge,agent,operators,release}` | 按能力维护的验收与回归入口 | 生产运行态或历史证据改写 |
| `docs/` | 当前架构、接口、运维和决策 | 历史证据的正文改写 |
| `runtime/` | 项目本地、非 Git 的可再生产物和运行状态 | V6 或宿主 Docker 的可写资源 |

历史证据、旧清单、候选 bundle 和生成报告由外部制品系统保存，不在源码树设置归档责任区。
P8 `runtime/p8/` 的部署身份只来自明确的 Direct Compose 配置；当前没有已配置实例。

`bin/ops` 是维护者的单一稳定入口。它只读取本项目的 `source/`、`runtime/` 与 V7 专属 Docker
约定；不会把 V6 代码、运行时或 Docker socket 当作后备资源。

`bin/ops cleanup` 由 `scripts/maintenance/ops.py` 直接装配本项目的 `cleanup_core.py` 与
`cleanup_policy.py`；通用的“计划 → 隔离 →
完整性验证 → 自动重建证明 → 独立永久删除”机制由 `cleanup_core.py` 负责，V7 候选、P8 阻断和发布实例 Compose 保护由
`cleanup_policy.py` 负责。它不得接管 Docker 卷、活动状态、P8、冷备、密钥或发布锁；P8 未裁决时
Docker 清理仍为硬性阻断。

维护检查的路径映射、实际命令和前置条件只在 `maintenance-gates.v2.json` 声明；
`check_runner.py` 负责选测和状态汇总，`project_environment.py` 负责项目内路径与子进程环境。
Shell 初始化委托本项目的解析器，不独立维护另一套路径默认值。
`cleanup_rebuild.py` 拥有固定重建步骤与证明绑定，`cleanup_policy.py` 决定候选可使用哪一步骤；
`cleanup_core.py` 在删除前消费证明并重新检查保护条件。证明不是发布验收，也不是对同一用户恶意改写源码的安全沙箱。
项目级入口由仓库内 `bootstrap-assets/` 模板生成；引导程序不覆盖已有不同内容的文件，不接管部署状态。

参数的所有者和启动解析点记录在 [parameter-register.md](parameter-register.md)。Contracts 的 runtime
context 只能承载已验证值，不能读环境或 Release Lock 文件；Platform、Execution、Agent、Data 与
Knowledge 的 composition root 才能把兼容的 `V7_*` 输入映射为 context。application、HTTP router、
Execution components/adapters、Data/Knowledge/Agent worker、Infrastructure service/database/worker adapter 以及
共享 health helper 不得新增环境读取、隐式 catalog/release 加载或未注入的 release header，
`check_architecture_boundaries.py` 会失败关闭。
