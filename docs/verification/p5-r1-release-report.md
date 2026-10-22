# P5-R1 / 0.7.0 精确成果身份发行报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-14T08:13:32.205314+00:00

## 完成内容

- TaskSpec 使用成果角色描述未来交付，Deliverable 将角色绑定到精确 Action、Execution、工作流节点、调用路径、scatter/数组位置、AssetRef、表示及摘要。
- Platform 重新查询执行、节点、发布回执和 Data manifest 后保存规范化引用；调用者提供的派生身份不能直接成为权威事实。
- VERIFIED 验收显式冻结当前分析登记。旧 `output_id` VERIFIED 记录保持可读，但不能发起新验收。
- CoveragePlan schema v2 冻结角色映射、精确输出和登记；旧 EXPLORATORY 报告明确包含 `LEGACY_OUTPUT_IDENTITY`。
- 新发布表示写入 `schema_id`；旧 P3/P4 资产通过固定兼容目录解析且不重写。

## 测试结果

- P5 聚焦测试：65 passed，0 failed，0 skipped。
- 完整 pytest：632 passed，0 failed，0 skipped。
- HTTP 响应丢失：1 passed。
- P1 规则：13 passed。
- 九个 wheel、九个 sdist、仓库外安装、真实 PostgreSQL 迁移和固定 checker 容器通过。

## 范围

本轮只完成 P5-R1。领域复算、空间检查、制图检查、结构化主张检查和多 checker 组合属于 P5-R2 / 0.7.1，未提前实现。
