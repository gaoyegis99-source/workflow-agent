# P5.3 / 0.6.2 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增 `open-science-v1`，以冻结 CoveragePlan 驱动六类检查，并保留 P3 验收行为。
- 新增固定 `open-science-check@1.0.0` 与受限 checker 容器；无法通用复算时明确返回 `INDETERMINATE`。
- 实现探索交付、验证交付、限定 OWNER 人工评审及检查预算。
- 保存观察来源中的精确 EvidenceRef；Knowledge 生命周期 outbox 支持租约、代次和 ack。
- 证据撤回会把相关 CoveragePlan 和 completion validity 标记为 `STALE`，历史报告与任务完成事实不变。

## 测试结果

- `uv run --locked pytest -q tests/p53`：12 passed。
- 全量命令为 592 passed；随后新增的内部 outbox HTTP 栅栏、登记歧义和 ack 丢失恢复用例均在定向套件通过，当前套件共 595 项。执行均为 0 failed、0 skipped。
- P1 规则：13 passed。
- 固定 `0.6.2` checker 容器：通过，容器报告与主机独立调用一致。
- 仓库外构建、安装、迁移、真实 HTTP、Worker、P3、Toil、QGIS 和制图 smoke：全部通过。

## 问题处理

- 健康响应的阶段枚举原先止于 P5.2，已扩展为 P5.3。
- Platform `0008` 的索引和唯一约束最初未同步到 SQLAlchemy metadata，导致迁移一致性检查失败；已补齐并回归通过。
- checker 容器测试输出目录最初受宿主 umask 影响，非 root 用户无法写入；测试脚本现显式设置临时输出目录权限，生产 Runner 的独立输出目录协议不变。

## 范围说明

P5.3 不自动证明现实观测真实性，不接受用户检查代码，也不把人工权限当作科学资格。当前通用复算仅报告能力覆盖；领域复算仍由登记检查器提供。本轮没有实现 Agent、模型调用、PDF/OCR、向量检索或 P6 功能。

## 阶段结论

P5.3 通过。P5 三轮完成，本轮结束，不自动进入 P6。

## 2026-10-14 逻辑审计补记

上面的测试数量和源码摘要是首次 `0.6.2` 验收的历史证据。后续审计发现并修复了生命周期事件名不一致、跨任务执行来源、研究变化后计划仍可能完成、通用检查过度判定、检查报告覆盖不完整、检查器 ERROR 被误归为科学 FAIL，以及任务查询未暴露完成有效性等问题。审计后的源码与原结构化证据摘要不同，不能继续用原 JSON 证明当前工作树；新的完整门禁和补丁发行应单独留证。具体完成度和剩余缺口见 [P5 逻辑审计](p5-logic-audit-report.md)。
