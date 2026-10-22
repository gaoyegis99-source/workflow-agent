# P5-R2.1 / 0.7.2 可信检查器组合发行报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-14T11:31:37.500142+00:00

## 完成内容

- 部署生成的只读 capability catalogue 同时冻结能力、契约和两个不可变 checker 镜像；Platform 与 Execution 启动时校验同一目录。
- CoveragePlan schema v3 按精确成果身份匹配固定能力。无匹配能力形成 `UNCOVERED / INDETERMINATE`，不能使用通用检查器兜底。
- 运行完整性先执行，领域复算通过后才运行结构化主张支持；每个实际启动的 checker 独立结算，未启动额度在取消、依赖失败或许可拒绝时释放。
- API、Platform Worker 和 Execution Worker 重启后，原 Acceptance、Action 和 Execution 身份保持不变。
- 真实 checker 容器被终止后，首次验收形成 ERROR；显式重试只启动失败能力和未运行下游，预算不重复结算。
- Task 修订与取消会阻止未启动 checker，释放剩余 envelope；已经启动的容器按真实执行事实结算。

## 测试结果

- P5 聚焦测试：86 passed，0 failed，0 skipped。
- 完整 pytest：653 passed，0 failed，0 skipped。
- HTTP 响应丢失：1 passed。
- P1 规则：13 passed。
- 真实 PostgreSQL `0009→0010`、九个发行包、仓库外安装、通用 checker 和 GDAL checker 容器通过。

## 范围

本轮完成 P5-R2.1 可靠性收口，不进入 P6。未知领域结果保持未覆盖；关联、预测和因果主张保持无法判定。
