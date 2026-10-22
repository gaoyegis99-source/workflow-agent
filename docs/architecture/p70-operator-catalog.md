# P7.0 Operator Catalog 与能力边界

P7.0 把能力固定分为三类：系统工具由 P6 Tool Catalog 管理，计算算子由 Operator Catalog 管理，验收检查器由 Checker Catalog 管理。三类能力不能相互解析。

`operator-catalog.json` 是部署生成的不可变锁。Platform 和 Execution 分别校验规范 JSON 摘要、算子语义摘要、实现摘要、固定镜像、协议和有界资源档位。Platform 在 Action 中冻结目录快照；Execution 在接收请求、创建容器和恢复时核对快照。Agent 同时冻结 Tool Catalog 和 Operator Catalog 摘要。生产部署必须让三者使用同一个 release lock。

生产模式不允许源码内置目录或占位镜像回退。Platform、Execution 和 Agent 启动时都校验 `V7_RELEASE_LOCK`、`V7_RELEASE_DIGEST`、发行版本及其所需目录；Execution 还校验六个实际运行镜像。任一文件、摘要、版本或镜像不一致都会使进程启动或 readiness 失败。开发模式保留源码目录，供单元测试和旧执行兼容使用，不能作为生产配置。

文件格式由 [`operator-catalog.schema.json`](../contracts/operator-catalog.schema.json) 描述；运行时还执行 JSON Schema 无法表达的全局唯一性、规范摘要和本地实现注册表一致性检查。

目录中的五个现有算子均标记为 `PLATFORM / BUILTIN / ACTIVE`。P7.0 没有加入独立算子。`CURATED_PACKAGE` 和 `DOMAIN_PACKAGE` 只定义来源边界，为后续分批试点保留。

0.10.1 不提供在线目录热更新。生命周期变更必须采用停机流程：停止准入，等待活动 Action、AgentRun 和发布收敛，停止 Platform、Execution 与 Agent，生成新的目录及 release lock，然后整体启动。`DISABLED` 阻止新 Action；`REVOKED` 用于阻止后续新启动和未开始的发布。已经原子提交的成果、历史 Action、预算和 provenance 不删除。因为活动执行必须先排空，本轮不承担“旧 release 的在途执行跨生命周期 release 恢复”的复杂仲裁。

生成部署锁：

```bash
V7_OPERATOR_CATALOG=/etc/v7/operator-catalog.json \
V7_OPERATOR_IMAGE=sha256:<digest> \
V7_SPATIAL_QGIS_IMAGE_ID=sha256:<digest> \
V7_SPATIAL_NATIVE_IMAGE_ID=sha256:<digest> \
uv run python scripts/build_operator_catalog.py generate
```

0.10.0 的历史组合保存在 [`p70-release-lock.json`](../verification/p70-release-lock.json)。0.10.1 的生产组合保存在 [`p701-release-lock.json`](../verification/p701-release-lock.json)，部署时必须整体使用，不能跨 release lock 拼接目录、包或镜像。

新 Agent 部署使用 `p7-science-tools-v2`，通过 `execution.submit_operator@2.0.0` 明确提交算子及版本。新 AgentRun 冻结 Tool Catalog 与 Operator Catalog 摘要，Worker 摘要不一致时拒绝推进。旧 P6 AgentRun 必须在升级前收敛，或继续由封存的旧 Worker 恢复；P7 Worker 不推进没有目录绑定的旧运行。

本地实现和远程实现采用判别联合。本地实现必须带固定镜像摘要；远程实现只带服务绑定和绑定修订，不再用伪造的 `image_digest` 冒充远程身份。
