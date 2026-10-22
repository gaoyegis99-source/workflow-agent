# P5.2 安装、迁移与运行

P5.2 版本为 `0.6.1`。Platform schema 升至 `0007_research_policy`；Data 保持 `0004_document_assets`，Execution 保持 `0006_remote_reliability`，Knowledge 保持 `0001_knowledge`。启动只检查版本，不自动迁移。

## 升级

等待活动执行和发布收敛后：

```text
停止 Platform API 和 Platform Worker
→ 安装九个 0.6.1 Python 发行包
→ uv run platform-admin migrate
→ uv run platform-admin check-schema
→ 核对 Data、Execution、Knowledge schema 未变化
→ 启动服务与 Worker
→ 运行 P5.2 定向测试和完整回归
```

迁移新增 `policy_decisions`、`analysis_revisions`、`analysis_registrations`、`claim_revisions` 和 `provenance_relations`。它不更新旧任务、Action、预算、成果、验收、Knowledge 正文或 P3/P4 来源。迁移可重复执行；从含旧任务的 `0006_service_binding_scope` 升级有专门回归。

回退前应停止创建 P5.2 记录并导出所需审计数据。降级到 `0006_service_binding_scope` 会删除上述五张 P5.2 表，因此只适用于明确放弃 P5.2 研究记录的回退；旧 P0–P5.1 数据不参与删除。

## 检查

开发期定向检查：

```bash
uv run --locked pytest -q tests/p52
```

统一门禁：

```bash
bash scripts/check_p5.sh --phase research
```

统一门禁执行锁定依赖、静态检查、真实 PostgreSQL 迁移、全部 pytest、P1 的 13 项规则、固定 P3/P4 容器构建和仓库外九包安装。仓库外 HTTP 冒烟会实际创建任务、分析修订和登记，再读取 Research 聚合视图。P5.1 的正文响应丢失回归继续保留。

P5.2 不需要新增服务、端口、数据库角色或存储卷。Platform 访问 Data 和 Knowledge 的现有内部 HTTP 地址与服务凭据，用于取得精确 AssetRef 和 EvidenceRef；凭据、令牌和正文不会写入研究关系或测试证据。
