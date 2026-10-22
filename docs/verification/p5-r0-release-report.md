# P5-R0 / 0.6.3 审计修复发行报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-14T05:34:06.105211+00:00

## 完成内容

- 九个工作区 Python 发行包统一为 `0.6.3`，生成九个 wheel 和九个 sdist。
- 固定 checker 镜像重建为 `groundwater-v7-checker:0.6.3`。
- 镜像 OCI revision 标签、发行证据和源文件清单共同指向 `sha256:e7d37827bfb1ebd4e729bf187762d2fd8d987cb2967754e77d18def3368c5423`。
- Platform、Data、Execution、Knowledge schema 保持 `0008`、`0004`、`0006`、`0002`，没有新增迁移。
- `0.6.2` 阶段报告和证据继续作为历史记录保留。

## 测试结果

- P5 定向测试：40 passed，0 failed，0 skipped。
- 完整 pytest：607 passed，0 failed，0 skipped。
- 真实 HTTP 响应丢失回归：1 passed。
- P1 规则回归：13 passed。
- 仓库外安装：九个 wheel/sdist、真实 PostgreSQL 迁移、API、Worker、Knowledge、P3/P4 容器闭环通过。
- 固定 checker 容器与主机独立调用生成完全相同的开放科学报告。

## 审计缺陷验证

- 历史 `IMPORTED` 事件能够继续消费并允许后续撤回传播。
- 跨任务 ExecutionRef 被拒绝。
- WITHDRAWN EvidenceRef 不能进入新研究修订。
- CoveragePlan 来源变化会使旧检查转为 `STALE`。
- 未登记领域能力保持 `INDETERMINATE`，不会伪装为 PASS。
- Task 查询返回当前 `completion_validity` 及修订。

## 范围

R0 是审计修复发行，没有新增领域检查器、精确成果角色或原任务重验。后续仍按 P5-R1 至 P5-R5 分轮实施。
