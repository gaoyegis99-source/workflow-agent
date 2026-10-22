# P6 / 0.9.2 完成度补强报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

Agent 可以在没有用户 Token 的情况下，经冻结目录和应用用例执行真实 Toil 工作流、真实 QGIS Buffer→Summary/Map 工作流，并将已发布成果规范化为精确角色绑定，随后启动 P5 runtime integrity 与 Decimal 独立复算并形成 VERIFIED Completion。新增迁移 `0018_agent_acceptance_identity` 允许内部 Agent Action 持有 checker 预算封套，同时保留 Agent、项目、任务和 CoveragePlan 审计身份。

运维侧增加受控 checkpoint 重建、Platform/checkpoint 分库存储备份恢复，以及可选的脱敏 OTLP trace。任务页可以创建带模型预算的 AGENT 任务，并禁用绕过 Agent 的人工执行按钮。真实 Chromium 已验证页面上传、语义、创建 AGENT、授权、两算子、发布、验收、报告和结构化回答；Token 不进入 URL 或浏览器持久存储。

A01–A15 由精确 pytest node ID 冻结，每项连续执行三次。评测器只接受三份 JUnit 中均实际通过的用例；文件存在或历史报告不能产生 PASS。

## 测试结果

- 最终 P6 门禁：287 项通过，0 失败、0 错误、0 跳过，用时 215.895 秒；另有真实 HTTP 回归 11 项通过。
- `uv run --locked pytest -q tests/p68/test_browser.py`：真实 Chromium 2 项通过，其中一项为完整 Agent 科学闭环。
- `uv run --locked pytest -q tests/p68 tests/p69 tests/architecture`：62 项通过，包含真实 Chromium 和 Agent 驱动的组合故障链。
- 组合故障链：模型提交响应丢失后由新 Agent Worker 查询原调用；随后同一工作流经历 Execution Worker 与 Toil leader 终止、HTTPS peer 重启和 Data 提交响应丢失，最终 Action、远端 Execution、预算和发布各保持唯一。
- 发行构建：0.9.2 的 10 个工作区包均生成 wheel 和源码包；其中 5 个 P6 边界 wheel（含 Platform API）连同锁定依赖在仓库外安装，AGENT 页面、Worker、工具目录、checkpoint 管理和备份恢复 CLI 全部通过。
- 确定性 A01–A15：15/15 PASS，所有用例运行 3 次，最小稳定性 100%；其中 A08/A09 共用同一条同时证明“冲突比较被阻止、无关分析不受阻”的策略测试。
- 真实模型：冻结 `deepseek-flash-2026-10-15` profile 对 A01–A15 各运行三次，45/45 正确；严重错误、质量门槛失败和 usage 缺失均为 0，共 45 次调用，费用 834,622 微单位。凭据没有进入目录、日志或证据。

## 问题处理

早期收口门禁中功能测试全部通过，但 JUnit 未包含 3 条 P5 金丝雀证据，评测器正确返回 FAIL。门禁已改为把策略、证据撤回和精确成果身份用例纳入同一 JUnit；继续补齐组合故障、真实 provider 和发行验证后，最终门禁为 287 项，A01–A15 确定性与真实模型评测均通过。

架构回归还发现 Agent 运维 CLI 直接依赖 SQLAlchemy、OpenTelemetry 未登记为适配层依赖。阻塞查询已下沉到基础设施仓储；架构白名单只增加部署侧 OpenTelemetry 适配器。

真实 provider 冒烟暴露了 Pydantic `JsonValue` 空 schema、Responses 实现要求顶层 `type`/`anyOf`、非 strict 服务仍需本地严格校验，以及分块保活可延长 socket timeout 四个问题。现已展开递归 JSON schema、保持 Pydantic 与工具目录的本地严格准入、加入单调墙钟读取期限，并禁止无查询能力的 stateless provider 在 UNKNOWN 后重新提交。真实评测支持最多 15 个有界并发，每个调用和一次格式修复分别计费。

## 范围与限制

组合故障链使用独立本机 HTTPS peer 和真实容器完成；此前 P4.3 的指定远端设备验收仍由 P4.3.2 阶段证据覆盖。公开 BFCL、τ-bench 和 ScienceAgentBench 当前只用于能力映射，不是本版本的通过证据。`deepseek-flash` 是供应商别名，目录摘要不能证明其背后权重永远不变。

## 阶段结论

0.9.2 的本地实现、恢复、真实容器、真实浏览器、发行安装、三次确定性评测和三次真实模型评测均通过。P6 完成并在本轮结束，不自动进入 P7。
