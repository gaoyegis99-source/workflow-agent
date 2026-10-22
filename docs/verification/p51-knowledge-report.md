# P5.1 / 0.6.0 Knowledge 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增独立 `groundwater-knowledge-service`，拥有 PostgreSQL、正文存储、迁移、API、Worker、租约、幂等和 outbox。
- Data 支持 Markdown/TXT 原始资产和专用文档 Worker；既有表格、空间及发布逻辑保持兼容。
- 实现 `utf8-nfc-lf-v1`、`paragraph-window-v1`、确定性 EvidenceUnit、精确 EvidenceRef 与读取回执。
- 实现 PostgreSQL 全文与 trigram 词法检索；SearchHit 与正式证据引用分离。
- 实现 ACTIVE/WITHDRAWN、DECLARED/CORROBORATED/CONFLICTING、UNREVIEWED/REVIEWED 三轴状态，以及 OWNER 撤回和历史精确读取。
- Platform 新增项目级 Knowledge 网关，每次请求重新检查当前身份和角色。

## 问题处理

- 真实 HTTP 测试发现内部导入视图被直接作为公开接受回执，因额外恢复字段触发 500。网关现显式转换为 `KnowledgeImportReceipt`。
- Data 暂时不可达原本会把导入写成永久失败。Worker 现按 generation 安全退回 `QUEUED`，恢复后继续原导入。
- 搜索查询新增空白与控制字符拒绝，并在执行前统一 NFC，保证摘要和排序输入稳定。

## 测试结果

- P5.1 定向测试：12 项通过，覆盖契约、规范化、分段、迁移、故障恢复、权限、真实 HTTP、10,000 单元检索质量与 p95 门槛。
- 实现后的首次完整 P0–P4/P5.1 回归：572 项通过，耗时 1,829.54 秒；其后新增的迁移、暂时故障和真实 HTTP 用例由最终统一门禁重新计数。
- 最终统一门禁：576 项通过、0 失败、0 错误、0 跳过；13 项 P1 规则通过。随后强化的真实 HTTP 响应丢失/Platform 重启用例定向复测 1 项通过。9 个发行包的仓库外构建、安装，Knowledge 独立迁移/启动，以及 P3/P4 容器闭环全部通过。结构化结果见 [P5.1 证据](p51-knowledge-evidence.json)。

## 范围与结论

本轮只实现 MD/TXT、精确引用、生命周期和词法检索。没有实现 P5.2 分析协议、主张、来源图与策略，也没有实现 P5.3 通用验收、人工评审和证据失效传播。Knowledge 内容默认为已声明且未评审，不能自动满足“已核实依据”。

P5.1 已通过并结束；不自动进入 P5.2。
