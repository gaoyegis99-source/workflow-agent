# Capability Studio A1 / 0.14.0 阶段报告

> HISTORICAL. A1 / 0.14.0 阶段记录；当前发布以机器可读 Release pointer 为准。

日期：2026-10-16。基线：P7.3a / 0.13.0。结论：**A1 通过，本轮结束，不进入 A2。**

## 完成内容

- 新增确定性 Authoring Descriptor，精确覆盖 11 个计算算子。语义摘要与展示摘要分离；
  Tool 与 Checker 不进入普通算子编辑目录。
- 新增项目级 WorkflowDefinition 与不可变 WorkflowRevision。创建操作原子生成服务端
  blank-workflow-v1 revision 1；A1 追加只能改变版本化元数据和布局。
- 新增 Platform 迁移 0019_authoring_drafts、仓储、事务用例和 7 条公开 HTTP 路由。写入
  复用当前身份校验及幂等记录，不创建 Action、预算、outbox 或 WorkflowPackage。
- 新增 apps/capability-studio React/TypeScript 前端，并由 Platform API 在 /studio/
  同源提供。Token 只保存在页面内存。
- 工作区 11 个 Python 包和 CWL runtime 统一为 0.14.0。release lock 继续使用 schema
  v2，并强制完整 wheel 证据及 Studio bundle 摘要。

## 测试结果

| 门禁 | 结果 |
|---|---|
| bash scripts/check_authoring.sh --phase drafts | 前端 typecheck、1 项 Vitest、生产构建、17 项 A1 pytest、Ruff 全通过 |
| bash scripts/check_p7.sh --phase catalog | 24 项 P7 目录测试、13 项 P1 规则、Ruff 全通过 |
| P0–P7 兼容集合 | 首次 918/919 通过；唯一失败为旧 OpenAPI 精确清单，加入 5 条已实现 A1 路由后定向重跑通过 |
| 定向最终回归 | OpenAPI 精确清单 + 全部 A1 共 18 项通过 |
| uv run --locked python scripts/verify_distribution.py | 11 wheel/sdist、核心标准库边界、仓库外安装、真实 PostgreSQL/HTTP、P3/P4/QGIS/制图及 0001 升级全部通过 |
| wheel 检查 | 11 个 0.14.0 wheel；Platform API wheel 内含 Studio index 与哈希资源 |

没有失败、错误或必测跳过项。P4 长节点性能矩阵没有重跑，因为 A1 未修改调度器、容器运行
或 Toil 配置。

## 已处理问题

1. 初版描述符只检测模型级 validator，漏掉字段级集合约束。现同时识别 field/model
   validators，并以三类已知 Schema 差异回归。
2. 初版描述符生成位于 core，违反核心包不依赖 wire contracts 的边界。最终投影移至
   contracts，core 只保留标准库领域规则与事务用例。
3. 旧 P7 生产锁测试缺少 wheel 与前端证据。测试样例升级为完整 schema v2 锁；生产规则
   没有放宽。
4. 旧 OpenAPI 精确路由清单未包含 A1 路由。清单已更新并定向重跑。

## 范围与限制

A1 支持算子发现、定义创建、修订历史、元数据/布局保存和基础网页操作。它不允许修改
workflow source，也没有节点画布、连接校验、候选冻结、正式验证、试运行、发布、提案或
算子注册。已有 /workflow-packages 和执行接口没有改变。

## 发行身份

- Platform schema：0019_authoring_drafts
- Data schema：0004_document_assets
- Execution schema：0006_remote_reliability
- Knowledge schema：0002_lifecycle_outbox
- Release digest：sha256:e5e8d00a9174e4d40a8900d0b85167e6948dc935eb61652c078c1a8497be116a
- Source tree digest：sha256:636879976f3645866e5748e24a0556a7a7fb86aa9d9773d97bb080d9bf3c7b93

结构化证据见 [a1-release-evidence.json](a1-release-evidence.json)，冻结锁见
[a1-release-lock.json](a1-release-lock.json)。
