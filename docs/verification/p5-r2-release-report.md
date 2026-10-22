# P5-R2 / 0.7.1 可信检查器组合发行报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-14T10:23:38.380979+00:00

## 完成内容

- 部署生成的只读 capability catalogue 同时冻结能力、契约和两个不可变 checker 镜像；Platform 与 Execution 启动时校验同一目录。
- CoveragePlan schema v3 按精确成果身份匹配固定能力。无匹配能力形成 `UNCOVERED / INDETERMINATE`，不能使用通用检查器兜底。
- 运行完整性先执行，领域复算通过后才运行结构化主张支持；每个实际启动的 checker 独立结算，未启动额度在取消、依赖失败或许可拒绝时释放。
- ERROR 重试只重跑失败能力，并仅在计划、来源和能力摘要完全一致时复用 PASS。
- P3 Decimal 复算、GDAL 空间有效性、PNG/模板/源事实检查和狭窄描述性数值主张已进入受信目录。

## 测试结果

- P5 聚焦测试：81 passed，0 failed，0 skipped。
- 完整 pytest：648 passed，0 failed，0 skipped。
- HTTP 响应丢失：1 passed。
- P1 规则：13 passed。
- 真实 PostgreSQL `0009→0010`、九个发行包、仓库外安装、通用 checker 和 GDAL checker 容器通过。

## 范围

本轮完成 P5-R2，不进入 P6。未知领域结果保持未覆盖；关联、预测和因果主张保持无法判定。
