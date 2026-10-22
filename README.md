# Groundwater Platform V7

当前实现 **P8.2 / 0.15.3 统一企业工作区与维护窗口升级**：Release Lock schema v3 锁定当前及 `0.15.2` 恢复所需的目录、镜像、运行配置和协议；活动 Toil 工作流可在安全停车后使用原 Action、节点和 JobStore 创建新 epoch 恢复。提交前失败恢复冷备份和源发布，提交后禁止直接自动降级。业务 schema 保持不变；上传、资产与知识文档目录使用带范围约束的游标分页。

当前激活的生产 Release 以 [P8.2 当前运行指针](docs/verification/p82-current-release.json) 为准；历史验收报告中的摘要只表示当次冻结的验收基线，不应当作在线实例发现机制。

当前 `0.15.3` 发布身份、1106 项全量回归基线和 2026-10-19 运行态复核见[当前基线审计](docs/verification/p82-0153-current-baseline-20261019.md)。只读观察发现 17 个运行容器中 1 个 `platform-worker` 健康检查失败；隔离端到端测试还发现资产超过 100 项时项目概览的游标处理错误。两个修复已打入 `0.15.5` 候选，并在隔离栈重验关键链路；候选源码摘要与 Release Lock 一致，但完整回归未取得权威结果，因此没有部署或切换当前指针。详见[候选验收报告](docs/verification/p8-closeout-0155-candidate-report.md)。

V7 使用统一 React 企业数据平台：根入口 `/` 连接临时令牌并选择项目，项目内统一提供概览、资产、任务、运行、工作流、发布和算子治理。旧 `/ui/*` 与 `/studio/*` 已删除。令牌只保存在 React 内存中，刷新后必须重新输入；`SAMPLE_VERIFIED` 仍不等同于科学 `VERIFIED`。

接口与身份规则见 [P5-R1 精确成果身份](docs/p5-r1-output-identity.md)，可信检查器目录与运行方式见 [P5-R2 能力契约](docs/p5-r2-checker-capabilities.md)和 [P5-R2 运维说明](docs/p5-r2-operations.md)。P3、P4、P5 历史记录保持兼容。Data/Knowledge 使用精确授权读取，写工具只调用冻结的既有用例；任意代码执行仍未开放。

P6 的框架调研、目标架构、模型与工具边界、故障矩阵及十轮实施顺序见 [P6 自主科学 Agent 实施方案](docs/architecture/p6-autonomous-scientific-agent-implementation-plan.md)。P6.0 的实际选择见 [运行时 ADR](docs/decisions/p6-runtime-selection.md)；P6.1 的状态与迁移见 [确定性 Agent 内核](docs/p61-agent-kernel.md)；P6.2 的运行和恢复语义见 [模型网关](docs/p62-model-gateway.md)。

## 安装与启动

Python 3.12、uv >=0.8.14、Docker Engine/Compose。完整依赖锁在 `uv.lock`；应用、计算和 PostgreSQL 镜像均以不可变 image ID 写入 Release Lock。

正式部署只使用 [P8.2 统一部署流程](docs/operations/p8-unified-deployment.md)。先构建不可变 Release；构建命令会输出唯一的 `.p8-runtime/releases/<version>-<release-digest>` 目录。将该完整输出路径传给配置器，不要按镜像标签或“最新目录”猜测发布物：

```bash
uv sync --locked
bash scripts/build_p8_release.sh

uv run --locked python scripts/configure_p8.py \
  --instance production \
  --release-dir .p8-runtime/releases/<version>-<release-digest> \
  --gateway-bind 127.0.0.1 \
  --gateway-port 18080 \
  --execution-port 18082 \
  --postgres-port 55450

bash scripts/p8.sh up .p8-runtime/instances/production/p8.env
bash scripts/p8.sh status .p8-runtime/instances/production/p8.env
```

工作区依赖边界与检查方式见[工作区依赖边界](docs/operations/workspace-dependencies.md)：V7 不加载
同一工作区中的旧平台源码；历史验证材料中保留的旧路径不构成运行依赖。
源码、运行态、实验与审计材料的保留规则见[项目生命周期](docs/operations/project-lifecycle.md)。

平台入口 `http://127.0.0.1:18080/`；OpenAPI `/api/v1/openapi.json`。当前受信局域网 Gateway 入口见 [运行指针](docs/verification/p82-current-release.json) 的 `gateway` 字段（本次审计为 `http://192.168.167.87:18080`）；局域网访问只开放 Gateway，并按 [P8.2 统一部署流程](docs/operations/p8-unified-deployment.md)限制来源子网。只支持宽度不低于 1100px 的桌面浏览器。P8 Compose 按固定顺序显式迁移四个数据库并通过 readiness 后才启动 Worker；API 进程本身不自动迁移。可用 [合成 GeoPackage](tests/p42/fixtures/points-utm.gpkg) 或[合成 CSV](tests/p3/fixtures/monitoring.csv) 完成演示；它们不是真实地下水观测。

## 验收

```bash
bash scripts/check_p8.sh --phase upgrade
# 维护窗口升级、停车与回滚见 docs/operations/p8-upgrade-rollback.md
bash scripts/check_p6.sh --phase runtime
# P7 表格算子、可信验收与完整兼容回归
bash scripts/check_p7.sh --phase tabular
bash scripts/check_p7.sh --phase tabular-acceptance
bash scripts/check_p7.sh --phase table-project
bash scripts/check_p7.sh --phase table-cast
bash scripts/check_p7.sh --phase table-aggregate
bash scripts/check_p7.sh --phase catalog
bash scripts/check_p7.sh --phase compatibility
# Capability Studio A6 项目版本库、正式 Release 执行与兼容门禁
bash scripts/check_authoring.sh --phase release-library
# P6.1 聚焦回归
bash scripts/check_p6.sh --phase kernel
# P6.2 模型网关聚焦回归
bash scripts/check_p6.sh --phase model
# 完整回归和仓库外 Agent Runtime 安装；不会重复 P4 长性能矩阵
bash scripts/check_p6.sh --phase all
# Embedding 默认失败关闭；只有冻结的 HTTPS Catalog 通过认证、制品和延迟门禁后才可启用
uv run --locked python scripts/check_embedding_gate.py \
  --catalog /absolute/path/to/enabled-embedding-catalog.json
bash scripts/check_p5.sh --phase all
bash scripts/check_p5.sh --phase reliability
bash scripts/check_p5.sh --phase identity
bash scripts/check_p43_reliability.sh --profile quick
# standard 还需配置真实 HTTPS 远端的 URL、Token 文件、CA 文件和实现摘要
bash scripts/check_p43_reliability.sh --profile standard
bash scripts/check_p432_reliability.sh --profile quick
# standard 另需受限的远端 peer 重启命令
bash scripts/check_p432_reliability.sh --profile standard
bash scripts/check_p43.sh --phase direct
bash scripts/check_p4.sh --phase cartography
```

制图入口保留 P0–P4.2.1 回归，并增加真实 QGIS Layout、独立 PNG 校验、三节点 Toil、原子发布、浏览器 Blob 预览和仓库外安装闭环，同时回归 13 项 P1 规则。所有数据库、进程和容器故障使用临时测试资源。当前结果见 [P4.2.2 报告](docs/verification/p422-cartography-report.md)和[结构化证据](docs/verification/p422-cartography-evidence.json)。

固定单机性能基线使用大量确定性合成数据，单独运行：

```bash
bash scripts/check_p4_performance.sh --profile standard
```

配置、指标含义和适用边界见 [P4.1 工作流性能测试](docs/p4-workflow-performance.md)。

R4.4 并发 Worker 队列回归使用：

```bash
bash scripts/check_p4_scheduler_r44_roles.sh --profile quick
bash scripts/check_p4_scheduler_r44_roles.sh --profile standard
```

quick 每条工作流使用 4 个固定 30 CPU 秒节点；standard 同时运行两条各含 8 个节点的工作流并执行三次。R4.4 保持 chaining 关闭且不修改图结构、身份、预算或公开接口。当前结果见 [R4.4 报告](docs/verification/p4-r44-role-split-report.md)和[结构化证据](docs/verification/p4-r44-role-split-evidence.json)。

R5 chaining 选型使用：

```bash
bash scripts/check_p4_scheduler_r5.sh --profile quick
bash scripts/check_p4_scheduler_r5.sh --profile standard
```

standard 会按平衡顺序对 off/on 各运行三次 8×30 CPU 秒工作流，并在性能测试前验证真实 Toil 的重启、JobStore 接管、取消和唯一结算。当前先以逻辑资格测试和完整回归收口；受仓库外负载影响的性能结果保留为待复测诊断。结果见 [R5 报告](docs/verification/p4-r5-chaining-report.md)和[结构化证据](docs/verification/p4-r5-chaining-evidence.json)。

## 内部边界

```text
apps/platform-api/               同源页面、公开 HTTP、鉴权与装配
packages/platform-core/          标准库领域规则、应用用例与端口
packages/platform-infrastructure/ Platform PostgreSQL、HTTP 适配、CLI、Worker
packages/contracts/              严格 DTO、错误封套与 JSON Schema
apps/data-service/               独立数据库、资产字节、导入 Worker、原子发布
apps/execution-service/          独立数据库、执行身份、容器 Worker、恢复
apps/knowledge-service/          独立数据库、正文、EvidenceUnit、检索和生命周期
apps/agent-worker/               Agent Runtime 适配；只接收 frame 并产生 proposal
runtimes/cwl/                    独立锁定的 Toil/cwltool 引擎与受信桥接器
packages/operator-pack/          五个平台算子；NumPy、固定 GDAL/QGIS 运行实现
packages/tabular-operator-pack/  独立审核表格包；Profile、Validate、Project、Filter、Cast 与 Aggregate
packages/checker-pack/           独立 Decimal 重算与固定开放检查；不能导入生产算子
```

Platform 不读取 Data/Execution/Knowledge 表；服务通过认证 HTTP 协作。长期动作由独立 Worker 推进。计算身份、实际执行、发布事实、任务清理分开记录；通信未知时保留预算占用。搜索命中只用于发现，精确读取才产生可复核 EvidenceRef。

P5 现已包含精确知识、研究协议、来源图、局部策略、开放验收和证据失效传播；不提供用户上传策略、规则代码或自然语言冲突裁决。PDF、DOCX、HTML、URL 抓取、OCR 和模型重排仍未实现。P6.10 提供受限 Embedding 客户端与诊断集，但尚未把向量检索接入生产 Knowledge 搜索。P4 的远程空间执行与制图能力保持原状。V6 仅作为算法来源参考，不加载它的代码、配置或运行资源。

P6 运维支持 `agent-admin rebuild-checkpoint` 和 `agent-ledger-backup backup|restore`。重建只允许在没有活动租约时执行；恢复要求显式 `--confirm-restore`，并校验 Platform 与 checkpoint schema、manifest 和各表摘要。设置 `V7_AGENT_OTEL_ENDPOINT` 可导出只包含运行、任务、代次和 proposal 身份的 OTLP trace；未设置时使用无导出 tracer，不影响 readiness 或业务执行。

## 文档

- [P6.1 / 0.8.1 确定性 Agent 内核与迁移](docs/p61-agent-kernel.md)
- [P6.1 / 0.8.1 阶段报告](docs/verification/p61-agent-kernel-report.md)
- [P6.1 / 0.8.1 结构化证据](docs/verification/p61-agent-kernel-evidence.json)
- [P6.2 / 0.8.2 模型网关与预算](docs/p62-model-gateway.md)
- [P6.2 / 0.8.2 阶段报告](docs/verification/p62-model-gateway-report.md)
- [P6.2 / 0.8.2 结构化证据](docs/verification/p62-model-gateway-evidence.json)
- [P6.3 / 0.8.3 上下文与只读 Agent](docs/p63-read-only-agent.md)
- [P6.3 / 0.8.3 阶段报告](docs/verification/p63-read-only-agent-report.md)
- [P6.3 / 0.8.3 结构化证据](docs/verification/p63-read-only-agent-evidence.json)
- [P6.4 / 0.8.4 受控单算子 Agent](docs/p64-controlled-agent.md)
- [P6.4 / 0.8.4 阶段报告](docs/verification/p64-controlled-agent-report.md)
- [P6.4 / 0.8.4 结构化证据](docs/verification/p64-controlled-agent-evidence.json)
- [P6.5 / 0.8.5 持久唤醒与恢复](docs/p65-wakeup-recovery.md)
- [P6.5 / 0.8.5 阶段报告](docs/verification/p65-wakeup-recovery-report.md)
- [P6.5 / 0.8.5 结构化证据](docs/verification/p65-wakeup-recovery-evidence.json)
- [P6.6 科学规划与工作流](docs/p66-science-agent.md)
- [P6.7 有限自主修复](docs/p67-bounded-repair.md)
- [P6.8 页面与运维](docs/p68-agent-ui-operations.md)
- [P6.9 评测与发行](docs/p69-evaluation-release.md)
- [P6.9 发行报告](docs/verification/p69-release-report.md)
- [P6.9 结构化证据](docs/verification/p69-release-evidence.json)
- [P6 / 0.9.2 完成度补强说明](docs/p6-completion-hardening.md)
- [P6 / 0.9.2 完成度补强报告](docs/verification/p6-completion-hardening-report.md)
- [P6 / 0.9.2 结构化证据](docs/verification/p6-completion-hardening-evidence.json)
- [P6.10 / 0.9.3 模拟 LLM 与 Embedding 诊断](docs/p610-simulated-embedding-diagnostics.md)
- [P6.10 / 0.9.3 测试报告](docs/verification/p610-simulated-embedding-report.md)
- [P6.10 / 0.9.3 结构化证据](docs/verification/p610-simulated-embedding-evidence.json)
- [P7.0 Operator Catalog 架构](docs/architecture/p70-operator-catalog.md)
- [P7.0 收口 / 0.10.1 部署与运行](docs/p701-operations.md)
- [P7.0 收口 / 0.10.1 阶段报告](docs/verification/p701-release-report.md)
- [P7.0 收口 / 0.10.1 结构化证据](docs/verification/p701-release-evidence.json)
- [P7.0 收口 / 0.10.1 Release Lock](docs/verification/p701-release-lock.json)
- [P7.0 / 0.10.0 历史报告](docs/verification/p70-operator-catalog-report.md)
- [P7.1a / 0.11.0 独立表格算子协议](docs/architecture/p71a-tabular-operators.md)
- [P7.1a / 0.11.0 部署与运行](docs/p71a-operations.md)
- [P7.1a / 0.11.0 阶段报告](docs/verification/p71a-release-report.md)
- [P7.1a / 0.11.0 结构化证据](docs/verification/p71a-release-evidence.json)
- [P7.1a / 0.11.0 Release Lock](docs/verification/p71a-release-lock.json)
- [P7.1b / 0.11.1 表格可信验收协议](docs/architecture/p71b-tabular-acceptance.md)
- [P7.1b / 0.11.1 部署与运行](docs/p71b-operations.md)
- [P7.1b / 0.11.1 阶段报告](docs/verification/p71b-release-report.md)
- [P7.1b / 0.11.1 结构化证据](docs/verification/p71b-release-evidence.json)
- [P7.1b / 0.11.1 Release Lock](docs/verification/p71b-release-lock.json)
- [P7.2a / 0.12.0 表格投影协议](docs/architecture/p72a-table-project.md)
- [P7.2a / 0.12.0 部署与运行](docs/p72a-operations.md)
- [P7.2a / 0.12.0 阶段报告](docs/verification/p72a-release-report.md)
- [P7.2a / 0.12.0 结构化证据](docs/verification/p72a-release-evidence.json)
- [P7.2a / 0.12.0 Release Lock](docs/verification/p72a-release-lock.json)
- [P7.2b / 0.12.1 表格过滤协议](docs/architecture/p72b-table-filter.md)
- [P7.2b / 0.12.1 部署与运行](docs/p72b-operations.md)
- [P7.2b / 0.12.1 阶段报告](docs/verification/p72b-release-report.md)
- [P7.2b / 0.12.1 结构化证据](docs/verification/p72b-release-evidence.json)
- [P7.2b / 0.12.1 Release Lock](docs/verification/p72b-release-lock.json)
- [P7.2c / 0.12.2 严格类型转换协议](docs/architecture/p72c-table-cast.md)
- [P7.2c / 0.12.2 部署与运行](docs/p72c-operations.md)
- [P7.2c / 0.12.2 阶段报告](docs/verification/p72c-release-report.md)
- [P7.2c / 0.12.2 结构化证据](docs/verification/p72c-release-evidence.json)
- [P7.2c / 0.12.2 Release Lock](docs/verification/p72c-release-lock.json)
- [P7.3 Join / Aggregate 架构复评](docs/architecture/p73-join-aggregate-reassessment.md)
- [P7.3 Join / Aggregate 结构化决定](docs/verification/p73-join-aggregate-decision.json)
- [P7.3a / 0.13.0 受限表格聚合协议](docs/architecture/p73a-table-aggregate.md)
- [P7.3a / 0.13.0 部署与运行](docs/p73a-operations.md)
- [P7.3a / 0.13.0 阶段报告](docs/verification/p73a-release-report.md)
- [P7.3a / 0.13.0 结构化证据](docs/verification/p73a-release-evidence.json)
- [P7.3a / 0.13.0 Release Lock](docs/verification/p73a-release-lock.json)
- [A1 / 0.14.0 草稿与描述符契约](docs/architecture/a1-authoring-contract.md)
- [A1 / 0.14.0 部署与回退](docs/a1-operations.md)
- [A1 / 0.14.0 阶段报告](docs/verification/a1-release-report.md)
- [A1 / 0.14.0 结构化证据](docs/verification/a1-release-evidence.json)
- [A1 / 0.14.0 Release Lock](docs/verification/a1-release-lock.json)
- [A2 / 0.14.1 编辑与校验契约](docs/architecture/a2-authoring-validation-contract.md)
- [A2 / 0.14.1 部署与回退](docs/a2-operations.md)
- [A2 / 0.14.1 阶段报告](docs/verification/a2-release-report.md)
- [A2 / 0.14.1 结构化证据](docs/verification/a2-release-evidence.json)
- [A2 / 0.14.1 Release Lock](docs/verification/a2-release-lock.json)
- [A3 / 0.14.2 候选、试运行与版本契约](docs/architecture/a3-authoring-release-contract.md)
- [A3 / 0.14.2 部署与回退](docs/a3-operations.md)
- [A3 / 0.14.2 阶段报告](docs/verification/a3-release-report.md)
- [A3 / 0.14.2 结构化证据](docs/verification/a3-release-evidence.json)
- [A3 / 0.14.2 Release Lock](docs/verification/a3-release-lock.json)
- [A4 / 0.14.3 算子接入材料契约](docs/architecture/a4-operator-registration-contract.md)
- [A4 / 0.14.3 部署与维护](docs/a4-operations.md)
- [A4 / 0.14.3 阶段报告](docs/verification/a4-release-report.md)
- [A4 / 0.14.3 结构化证据](docs/verification/a4-release-evidence.json)
- [A4 / 0.14.3 Release Lock](docs/verification/a4-release-lock.json)
- [A5 / 0.14.4 高级 CWL 编辑契约](docs/architecture/a5-advanced-cwl-contract.md)
- [A5 / 0.14.4 部署与回退](docs/a5-operations.md)
- [A5 / 0.14.4 阶段报告](docs/verification/a5-release-report.md)
- [A5 / 0.14.4 结构化证据](docs/verification/a5-release-evidence.json)
- [A5 / 0.14.4 Release Lock](docs/verification/a5-release-lock.json)
- [A6 / 0.14.5 项目版本库与正式执行契约](docs/architecture/a6-workflow-release-library.md)
- [A6 / 0.14.5 部署与回退](docs/a6-operations.md)
- [A6 / 0.14.5 阶段报告](docs/verification/a6-release-report.md)
- [A6 / 0.14.5 结构化证据](docs/verification/a6-release-evidence.json)
- [A6 / 0.14.5 Release Lock](docs/verification/a6-release-lock.json)
- [A7 / 0.14.6 工作流版本生命周期契约](docs/architecture/a7-workflow-release-lifecycle.md)
- [A7 / 0.14.6 部署与回退](docs/a7-operations.md)
- [A7 / 0.14.6 阶段报告](docs/verification/a7-release-report.md)
- [A7 / 0.14.6 结构化证据](docs/verification/a7-release-evidence.json)
- [A7 / 0.14.6 Release Lock](docs/verification/a7-release-lock.json)
- [P6.0 / 0.8.0 Agent 运行时选型 ADR](docs/decisions/p6-runtime-selection.md)
- [P6.0 / 0.8.0 阶段报告](docs/verification/p60-runtime-selection-report.md)
- [P6.0 / 0.8.0 结构化证据](docs/verification/p60-runtime-selection-evidence.json)
- [P5-R1 / 0.7.0 精确成果身份契约](docs/p5-r1-output-identity.md)
- [P5-R1 / 0.7.0 安装、迁移和运行](docs/p5-r1-operations.md)
- [P5-R2 / 0.7.1 可信检查器能力契约](docs/p5-r2-checker-capabilities.md)
- [P5-R2 / 0.7.1 安装、迁移和运行](docs/p5-r2-operations.md)
- [P5-R2.1 / 0.7.2 真实组合检查可靠性](docs/p5-r21-reliability.md)
- P5-R1 发行报告与结构化证据由统一门禁生成
- [P5-R0 / 0.6.3 审计修复发行报告](docs/verification/p5-r0-release-report.md)
- [P5-R0 / 0.6.3 结构化证据](docs/verification/p5-r0-release-evidence.json)
- [P5.3 开放科学验收契约](docs/p53-open-acceptance-api.md)
- [P5.3 安装、迁移和运行](docs/p53-operations.md)
- [P5.3 阶段报告与结构化证据](docs/verification/p53-open-acceptance-report.md)
- [P5.2 研究协议、来源与策略契约](docs/p52-research-api.md)
- [P5.2 安装、迁移和运行](docs/p52-operations.md)
- [P5.2 阶段报告与结构化证据](docs/verification/p52-research-report.md)
- [P5.1 Knowledge API 与证据契约](docs/p51-knowledge-api.md)
- [P5.1 安装、迁移和运行](docs/p51-operations.md)
- [P5.1 阶段报告与结构化证据](docs/verification/p51-knowledge-report.md)
- [P4.3.1a 远程可靠性方案](docs/p43-reliability-hardening-plan.md)
- [P4.3.1a 远程执行契约](docs/p43-remote-execution.md)
- [P4.3.1a 安装、升级和运行](docs/p43-operations.md)
- [P4.3.1a 阶段报告](docs/verification/p431a-reliability-report.md)
- [P4.3.2 混合工作流方案](docs/p432-mixed-workflow-plan.md)
- [P4.3.2 阶段报告](docs/verification/p432-mixed-workflow-report.md)
- [P4.3.2 可靠性补强报告](docs/verification/p432-reliability-report.md)
- [P4.2.2 静态制图契约](docs/p422-cartography-api.md)
- [P4.2.2 安装、升级和运行](docs/p422-cartography-operations.md)
- [P4.2.2 阶段报告与结构化证据](docs/verification/p422-cartography-report.md)
- [P4.2.1 空间接口、manifest 和工作流语义](docs/p42-spatial-api.md)
- [P4.2.1 安装、迁移和运行](docs/p42-spatial-operations.md)
- [P4.2.1 阶段报告与结构化证据](docs/verification/p42-spatial-report.md)
- [P4.1 工作流接口与恢复语义](docs/p4-workflows-api.md)
- [P4.1 安装、迁移和运行](docs/p4-workflows-operations.md)
- [P4.1 阶段报告与结构化证据](docs/verification/p4-workflows-report.md)
- [P4.1 工作流性能测试](docs/p4-workflow-performance.md)
- [P4.1 大规模合成工作流性能报告](docs/verification/p4-workflow-performance-report.md)
- [P4.1 R1 调度诊断契约](docs/p4-workflow-scheduler-diagnostics.md)
- [P4.1 R1 长节点诊断报告与结构化证据](docs/verification/p4-workflow-scheduler-diagnostics-report.md)
- [P4.1 R2 调度对照报告](docs/verification/p4-r2-scheduler-report.md)
- [P4.1 R2 调度结构化证据](docs/verification/p4-r2-scheduler-evidence.json)
- [P4.1 R3 进程生命周期报告](docs/verification/p4-r3-lifecycle-report.md)
- [P4.1 R3 进程生命周期结构化证据](docs/verification/p4-r3-lifecycle-evidence.json)
- [P4.1 R4.4 Worker 队列拆分报告](docs/verification/p4-r44-role-split-report.md)
- [P4.1 R4.4 Worker 队列拆分结构化证据](docs/verification/p4-r44-role-split-evidence.json)
- [P4.1 R5 Toil chaining 对照报告](docs/verification/p4-r5-chaining-report.md)
- [P4.1 R5 Toil chaining 结构化证据](docs/verification/p4-r5-chaining-evidence.json)
- [P3 API、科学契约与恢复语义](docs/p3-api.md)
- [P3 安装、升级和运行](docs/p3-operations.md)
- [P2 补齐验收历史](docs/verification/p2-followup-report.md)、[首次验收](docs/verification/p2-report.md)
- [P2 接口](docs/contracts/p2-control-contracts.md)、[身份管理 CLI](docs/operations/p2-database.md)
- [阶段路线](docs/implementation-plan.md)、[目标架构](docs/architecture/v7-architecture-and-interface-contracts.md)
- [P1 选型与历史结论](docs/decisions/p1-execution-components.md)
