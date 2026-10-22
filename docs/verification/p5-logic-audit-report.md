# P5 逻辑与完成度审计（2026-10-14）

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 结论

P5 的服务边界和审计骨架已经成立：P5.1 可保存并精确引用正文，P5.2 可冻结研究协议、主张、来源和策略，P5.3 可生成 CoveragePlan、运行固定检查、记录人工评审并传播证据撤回。它还不是“通用科学验证已经完成”的状态。当前适合称为**开放科学控制骨架可用，领域验证能力待补齐**。

完成度按计划能力评估：P5.1 约 90%，P5.2 约 80%，P5.3 约 65%，P5 整体约 78%。百分比表示计划条目的工程覆盖程度，不表示科学真实性概率。

## 本次关闭的问题

1. Knowledge 导入曾写 `IMPORTED`，Platform 只接受 `CREATED/WITHDRAWN`，旧导入事件可能阻塞后续撤回。新导入统一写 `CREATED`，消费者兼容历史 `IMPORTED`。
2. Execution 来源原先只检查项目，可能引用同项目其他任务的执行。现在同时检查 Task 身份。
3. 新研究记录曾可引用已经撤回的 EvidenceRef。网关现在核对当前生命周期并拒绝把 WITHDRAWN 证据写入新修订。
4. `DATA_VALIDITY` 曾因 manifest 存在而 PASS。现在技术完整性与领域数据有效性分开；没有领域检查器时返回 `INDETERMINATE`。
5. `CLAIM_SUPPORT` 曾可能用与该主张无关的观察来源 PASS。现在要求每个 ClaimRevision 都有指向自身的 `SUPPORTED_BY` 关系；即使存在关系，没有领域支持检查器仍返回 `INDETERMINATE`。
6. 检查提交后新增研究事实，旧 CoveragePlan 仍可能完成任务。现在自动回写和人工评审前均重算 source digest；不一致转为或拒绝为 STALE。
7. 检查器结果曾未强制完整覆盖自动检查项，且 ERROR 会变成科学 FAIL。现在校验计划摘要、检查项集合、类型、状态、理由和汇总状态；ERROR 保持 ERROR，可按既有规则显式重试。
8. 证据依赖曾只登记 OBSERVED 来源，影响策略判断的 DECLARED 精确证据撤回后可能不传播。现在冻结计划涉及的两类精确 EvidenceRef 都登记为失效依赖。
9. 多次正式登记中，已被后继分析修订取代的旧登记曾造成 VERIFIED 永久歧义。CoveragePlan 现在排除明确被替代的登记；并行且都有效的登记仍拒绝为歧义。
10. `GET task` 过去只有历史 `COMPLETED`，看不到当前验证已经 STALE。现在增加只读 completion validity 及修订号。

## 分轮完成度

| 轮次 | 已完成 | 仍缺 |
| --- | --- | --- |
| P5.1 Knowledge | MD/TXT、独立库与正文目录、NFC/LF 规范化、确定性分段、精确字符引用、读取回执、PostgreSQL 词法检索、撤回、租约/outbox | 没有 CORROBORATED/CONFLICTING/REVIEWED 的管理流程；10,000 单元门槛是本机合成基准，不代表生产容量 |
| P5.2 Research/Policy | 不可变分析修订、登记水位、ClaimRevision、类型化来源、执行准入策略、地下水比较局部策略 | MethodRef 仍是冻结声明而非受信方法目录；高程/基准转换只核对“有来源的声明”，没有验证转换目标、参数和数值；多个并行有效登记没有验收请求级显式选择 |
| P5.3 Acceptance | 探索/验证等级、固定 CoveragePlan、检查预算、人工评审范围、历史报告与当前有效性分离、撤回传播 | 缺少 DATA_VALIDITY、RESULT_REPRODUCTION、CLAIM_SUPPORT 的领域检查器注册与组合；required output 仍是通用 ID，不能表达精确工作流端口和数组位置；已完成且 STALE 的任务没有原任务内重验流程；除撤回外的知识状态变化没有事件入口 |

计划要求的 RO-Crate、W3C PROV 和 OpenLineage 字段映射文档也尚未单独交付；现有类型化关系可作为映射输入，但不能把参考架构文档中的概念说明视为正式映射成果。

## 科学能力边界

- `open-science-v1` 可以可靠说明检查了什么、缺了什么，并阻止未知能力伪装为 PASS。
- P3 的 `p3-timeseries-v1` 仍是当前唯一真正独立重算数值的科学检查器。
- 对任意新领域结果，P5.3 目前只能验证平台运行/发布身份和结构化策略前提；不能判断数值正确、数据符合领域质量规则或文字主张得到结果支持。
- 人工评审保存资格声明、准则和理由，但平台不验证评审人的外部资质。
- Knowledge 能证明“引用了哪段已保存正文”，不能证明正文陈述或现实观测为真。

## 后续优先级

1. 建立受信检查器目录和 Check Capability 契约，让检查计划只能把实际覆盖的数据类型、方法和主张映射到 PASS。
2. 为 required output 使用精确 `ExecutionRef + output port + array path + digest`，避免同名输出误覆盖。
3. 增加已完成任务的 REVALIDATION 生命周期，使用新 Acceptance 保留原 Completion，同时允许把当前 validity 恢复为 CURRENT。
4. 为 Knowledge 增加经过授权的 epistemic/review 状态修订及 outbox 事件，补齐非撤回状态变化传播。
5. 把 P5.3 故障矩阵补到独立 checker Worker 重启、响应丢失、旧代次、预算占用、取消竞争和完整真实 HTTP 闭环，再发布补丁版本。

## 验收说明

首次 `0.6.2` 证据保留为历史记录。审计修复已作为 P5-R0 / `0.6.3` 正式封存；当前源码、九个发行包、固定 checker 镜像和结构化证据共同指向同一源码摘要。完整结果见 [P5-R0 发行报告](p5-r0-release-report.md)和[结构化证据](p5-r0-release-evidence.json)。

本次实际验证：

- `uv run --locked pytest -q tests/p51 tests/p52 tests/p53`：40 passed。
- `uv run --locked pytest -q --tb=short`：607 passed，0 failed，0 skipped，耗时 1,590.54 秒。
- `uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v`：13 passed。
- `uv run --locked ruff check .` 与 `uv run --locked ruff format --check .`：通过，376 个文件格式一致。
- 九个 wheel 与九个 sdist 已在仓库外安装并运行；真实 PostgreSQL 迁移、API、Worker、Knowledge、P3/P4 容器闭环通过。
- 固定 `groundwater-v7-checker:0.6.3` 镜像与发行证据共同指向源码摘要 `sha256:e7d37827bfb1ebd4e729bf187762d2fd8d987cb2967754e77d18def3368c5423`。
