# 证据型语义检索与协作助手（首期）

## 边界

V7 的语义检索是**发现候选**的独立能力，不能改变既有词法检索、项目角色、
精确 `EvidenceRef`、预算、验收、执行参数或事实判定。任何候选都必须通过现有
`GET .../units/{unit_id}` 精确读取后，才可以成为回答中的引用。

首期不连接 V6 资料、V6 运行态、V6 Docker 或隐式数据通道；所有请求继续在当前
项目和当前授权的 Knowledge source 范围内运行。没有新建 P8 实例，也没有承诺生产
部署或自治 Agent。

## 版本化语义索引

`SemanticIndexProfile` 将以下全部纳入身份：Embedding 模型 ID/制品摘要、维度、
分段器 ID/摘要和文档投影版本。`profile_digest` 的变化必须创建新索引，不能就地
改变已排序候选的含义。

Knowledge 服务新增三类内部/public-gateway 读取路径：

| 路径 | 输出 | 约束 |
| --- | --- | --- |
| `POST .../semantic-search` | 语义候选 | 每项 `exact_read_required=true`，没有 `EvidenceRef`。 |
| `POST .../semantic-search-scoped` | 按冻结 Asset grant 限定的候选 | 给 Agent 使用；空 grant 不会退化为全项目搜索。 |
| `POST .../fusion-search` | 词法 + 语义 RRF 候选 | 返回通道来源，仍不是引用。 |
| `POST .../semantic-indexes/{index_id}/rebuild`（仅内部服务） | 显式重建索引 | 必须已有受信 Encoder；公开读取端点不会触发构建。 |

索引构建由注入的受信 `EmbeddingPort` 和 `SemanticIndexer` 显式触发；默认服务没有
Embedding 适配器时返回 `INDEX_UNAVAILABLE/ENCODER_UNAVAILABLE`，不会回退到任意
网络端点。文档导入、版本更新或撤回会将项目索引标为 `STALE`；撤回的向量同时被
失效，搜索失败关闭至重建完成。构建提交时会再次比较完整活动 Unit 集；任一文档在构建期间
新增、更新或撤回都会保持 `STALE`，而不是让旧向量变为 `READY`。词法检索与精确读取不受影响。

`knowledge_semantic_indexes`/`knowledge_semantic_vectors` 记录索引身份、状态、失败类别、
向量对应的正文摘要与生命周期修订；它们不保存模型请求、密钥或外部服务错误正文。
迁移 `0003_semantic_discovery` 只进入源码和临时数据库测试，未对任何生产 PostgreSQL
执行 apply。

## 证据型协作助手

`EvidenceCollaborationResponse` 仅允许四类输出：

- `EVIDENCED_FACT`：必须包含同一项目的精确 `EvidenceRef`、read receipt 和当前生命周期修订；
- `HYPOTHESIS`：显式不确定，不能标为高置信事实；
- `INSUFFICIENT_EVIDENCE`：没有引用且置信度为未确定；
- `ANALYSIS_RECOMMENDATION`：提出下一步分析，但不形成执行命令。

`ConfirmationProposal` 只是审阅项，强制
`requires_human_confirmation=true` 和 `execution_allowed=false`。它不能写入、发布、撤回、
消耗预算或调用工具。`validate_collaboration()` 会把每个引用与本轮活动 exact-read
回执比对；语义/词法命中、已撤回资料、跨项目 EvidenceRef 和模型自行编造的引用都会被拒绝。

Agent 要使用语义发现时必须选择新的只读目录
`p6-evidence-tools-v1`。旧 P6/P7 工具目录保持不变，因此现有部署不会因为源码更新而
隐式得到模型驱动发现能力。该工具的结果被标记为不可信发现内容；下一步只能是
`knowledge.read_evidence` 或向用户提出澄清/待确认提案。

## 六个月验收路线

| 月份 | 可验证交付 | 必须拒绝 |
| --- | --- | --- |
| 1–2 | 冻结项目级知识/Agent 评测集、索引合同、撤回/越权/提示注入用例 | 以 Embedding 命中授权或判定事实 |
| 3–4 | 在隔离环境构建版本化索引、精确回读与融合检索评测 | 跨项目候选、过期索引和无 exact-read 引用 |
| 5–6 | Studio 的知识搜索、证据阅读、提案审阅/确认闭环和版本化评测报告 | 自治执行、任意工具、跨项目共享或多 Agent 自治 |

Capability Studio 已提供项目内的融合候选搜索和精确证据阅读页；用户必须显式提供冻结的
索引 ID/档案摘要，页面不会创建索引、猜测身份或把候选变成引用。现有任务页继续显示
Agent 的受治理决策；专用的协作提案提交/确认 HTTP 用例尚未启用，因此
`EvidenceCollaborationResponse` 目前只能作为已验证的审阅 DTO，不能被前端伪造为执行命令。

真实模型/Embedding 质量报告、相关性、引用完整性、撤回、越权、提示注入、重复提案、
成本、延迟和拒绝行为都必须独立版本化。当前代码交付合同、候选索引生命周期、Studio
发现/精确阅读和协作输出验证；没有冻结评测集或受信 Embedding catalog 时，语义能力保持
不可用而非伪造通过。
