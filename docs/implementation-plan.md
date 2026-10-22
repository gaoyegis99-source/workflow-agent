# V7 分阶段开发与测试入口

采用用户确认的顺序：限定范围 → 开发 → 测试 → 修复 → 必要回归 → 阶段报告 → 结束本轮。阶段未通过时留在该阶段修复；通过后也不在同一轮自动进入下一阶段。V6 的代码、部署和运行数据保持原状。

本文件约束实施顺序；旧设计文档中的里程碑描述或组件建议若与此不一致，以本文件和用户确认的阶段范围为准。

算子与工作流注册编辑模块采用独立的 [Capability Studio 分轮实施计划 v1.3](architecture/capability-studio-implementation-plan.md)、[评估反馈与修订记录](architecture/capability-studio-design-review.md)及[开源调研复评](architecture/capability-studio-research-assessment.md)。A7 / 0.14.6 已在 A6 正式执行上补齐 Release 生命周期、节点许可与发布仲裁；条件能力当前承诺见[范围 ADR](decisions/v7-conditional-capabilities-scope.md)，不自动启动 Join、P8、市场或 Agent 编写工作流。

| 阶段 | 开发与验收范围 | 当前状态 |
| --- | --- | --- |
| P0 工程骨架与最小契约 | 独立安装／启动、依赖锁、核心与 HTTP 边界、首批类型、健康检查、契约正反例、无 V6 运行依赖 | 已完成，本轮回归通过 |
| P1 执行组件验证 | CWL 候选、隔离环境；步骤身份、提交查询、重启定位、取消、隔离及选型记录 | 已完成并通过，选择 Toil，见 P1 阶段报告 |
| P2 控制核心 | Task、授权、预算、Action、幂等、outbox、租约、命令查询；并发、重复、冲突、旧 Worker、取消、重启；外部计算模拟 | 0.1.1 补齐及重新验收通过，见 P2 补齐报告 |
| P3 单算子完整闭环 | Data、完整率与线性趋势、轻量执行、发布、独立验收和任务页；数值参考、拒绝错误结果、响应丢失、发布／取消竞争 | 0.2.1 补齐并通过：316 项 pytest、13 项 P1 规则、8 包独立安装，见 P3 补齐报告；0.2.0 首次报告保留为历史记录 |
| P4 工作流与异构执行 | P4.1 冻结 CWL、Toil、本机工作流、多成果发布与恢复；P4.2 QGIS/制图；P4.3 远程执行 | **P4.3.2 补强 `0.5.3` 已实现：远程输出隔离核验、父级发布复核、节点/远程账本原子性、双许可、取消/404 保守对账及三进程组合恢复。**见 [0.5.3 报告](verification/p432-reliability-report.md)、[原 0.5.2 报告](verification/p432-mixed-workflow-report.md)与[远程契约](p43-remote-execution.md)。 |
| P5 开放式科学策略与知识 | P5.1 Knowledge 与精确引用；P5.2 不可变协议、登记、主张、类型化来源和局部策略；P5.3 CoveragePlan、探索/验证交付、人工评审和撤回传播。详见 [P5 方案](architecture/p5-open-science-and-knowledge-plan.md) | 三轮均已有实现；P5-R0 / `0.6.3` 审计修复发行通过 607 项完整回归、仓库外安装、真实迁移和固定 checker 容器核验。`0.6.2` 原阶段证据保留；领域检查器和已完成任务重验仍待后续分轮补齐。见 [R0 报告](verification/p5-r0-release-report.md)与[P5 逻辑审计](verification/p5-logic-audit-report.md) |
| P6 自主 Agent | RuntimePort、上下文、工具、模型预算、唤醒和修复；工具评测、澄清、伪造结果、修订、恢复和真实模型。详见 [P6 分轮实施方案](architecture/p6-autonomous-scientific-agent-implementation-plan.md) | **P6 / `0.9.3` 已完成并补强：真实 Toil/QGIS/P5 闭环、组合故障、确定性与真实模型门禁均通过；模拟 LLM 完整复测和三模型 Embedding 诊断也已通过。**见 [P6.10 报告](verification/p610-simulated-embedding-report.md)。 |
| P7 能力迁移 | 系统工具、计算算子和 checker 分界；平台审核的独立算子分批接入。 | **P7.3a / `0.13.0` 已通过受限 Aggregate 验收。** 单输入聚合复用统一目录、预算、恢复、工作流、发布和独立复算链路；Join 仍未实现。见 [阶段报告](verification/p73a-release-report.md)、[聚合协议](architecture/p73a-table-aggregate.md)与[复评决定](architecture/p73-join-aggregate-reassessment.md)。 |
| P7 能力分批迁移 | 只迁移有明确通用价值的高质量算子；每批独立回归并结束本轮 | P7.3a 完成后结束本轮；只有存在真实 Join 用例时才进入 P7.3b 多输入基础，完成后再评估 P7.3c 的 INNER/LEFT、禁止多对多 Join 试点 |
| Capability Studio | 算子描述、工作流草稿、校验、试运行、发布、接入材料、正式版本使用及生命周期治理 | **A7 / 0.14.6 已实现：ACTIVE/DISABLED/REVOKED 修订、准入与节点仲裁、按 Action 持久发布许可及 Studio OWNER 控制。** Platform schema 为 `0023_workflow_release_lifecycle`。见 [A7 契约](architecture/a7-workflow-release-lifecycle.md)。 |
| P8 发布验收 | 性能、部署、备份恢复、升级回退；固定负载、故障组合、干净部署及完整业务回归 | **P8.1 / P8.2 当前生产版本为 `0.15.3`；P8.3 性能、资源及组合故障门禁和 P8.4 安全扫描、签名、离线发行门禁仍未开始。** 2026-10-19 只读运行态复核为 16/17 容器健康，`platform-worker` 因空闲时心跳未续写而被标记 unhealthy。`0.15.5` 候选在 17/17 健康的隔离栈通过 101 项浏览器分页、Direct、Workflow/StaticMap、权限及依赖恢复链路；源码摘要与 Release Lock 对齐。全量回归因独立 V6 测试并行而中断，候选未部署，G0/G1 仍为 PARTIAL。见[当前基线审计](verification/p82-0153-current-baseline-20261019.md)、[0.15.5 候选验收报告](verification/p8-closeout-0155-candidate-report.md)、[P8.1 报告](verification/p81-release-report.md)、[P8.2 升级报告](verification/p82-release-report.md)和[当前发布指针](verification/p82-current-release.json)。当前细化收口项见 [V7 完成度执行清单](v7-completion-checklist.md)。 |

CWL 选型：Toil 与 Weaver 均通过时选择 Toil；仅 Toil 通过时选择 Toil；仅 Weaver 通过时采用 Weaver；均失败时阻断工作流集成。隔离验证失败也不能通过 P1。具体结果见 [P1 选型记录](decisions/p1-execution-components.md)。首个 Agent 使用薄 SDK 适配，其他运行时独立评测后再决定替换。

首个可用业务版本在 P3 形成：无需模型即可经 API 完成计算、发布、验收和恢复。P0 的健康检查不构成这个业务版本。

每轮阶段报告必须包含实际完成内容、测试命令和结果、问题修复与缺口、支持范围与未执行项，以及通过或留在当前阶段的结论。P2 的 221 项覆盖与历史结果保留在 [P2 补齐报告](verification/p2-followup-report.md)，P3 历史结果见 [P3 补齐报告](verification/p3-followup-report.md)。P4.1 历史入口为 `bash scripts/check_p4.sh --phase workflows`；P4.2.1 入口为 `--phase spatial`；P4.2.2 入口为 `--phase cartography`；P4.3 入口为 `--phase remote`。P5 分项入口为 `bash scripts/check_p5.sh --phase knowledge|research|acceptance`，P5-R0 完整发行入口为 `--phase all`。P1 重型 Weaver/gVisor 实验保留历史证据；当前回归 13 项规则，并真实运行 Toil、QGIS 和 Native GDAL。
