# P8.2 / 0.15.3 测试发现修复与发布报告

> **HISTORICAL / SUPERSEDED。** 本页记录旧的 `sha256:c601610...` 发布和旧轮次测试数字；不能代表当前运行实例。当前 0.15.3 活动身份为 `sha256:ab0a7c60...d6b3b1`，当前全量回归为 1106/1106，线上运行态为 16/17 容器健康。现行身份与只读运行态见[当前基线审计](p82-0153-current-baseline-20261019.md)和[机器指针](p82-current-release.json)。

日期：2026-10-17

## 结论

五项既有发现均已修复并随不可变 `0.15.3` Release 部署到 `production-final`。当前 LAN 入口为 `http://192.168.167.87:18080`，loopback 入口为 `http://127.0.0.1:18080`。

- Release digest：`sha256:c601610702107888cd829fb058f83def51e8f38da77c61237ec00dbe54004e8d`
- 应用镜像：`sha256:92c52db8ac303ff2a52b43c8e41eb3d1fc714b0e637cc4462e23e104a7e262b3`
- CWL 镜像：`sha256:1675ed5f9d96f6a14dbc66b1eae9c17e97cf1f09db75dcba3d9b6bf65ec2e820`
- 源码摘要：`sha256:43e6b35bc23974df80181d8801966168445c3a61e7eaf7cc3db2f090d9ecd45d`
- Release 目录：`.p8-runtime/releases/0.15.3-c601610702107888cd829fb058f83def51e8f38da77c61237ec00dbe54004e8d`
- 升级操作：`.p8-runtime/instances/production-final/upgrades/0.15.3-c6016107`，最终状态 `COMMITTED`
- N-1：`0.15.2 / sha256:598c857ba69ddb285df96581f3e49627076e78a89818d6a609ee7affb2fc2f49`

当前权威运行指针见 [p82-current-release.json](p82-current-release.json)。

> **范围说明（2026-10-18 复核）：** 本页是 `0.15.3` 产品修复与维护切换记录，不是全项目测试门禁。历史合并报告和中间重跑均保留失败尝试；修复 Toil 协调目录、P68 Agent/UI 时序并补齐认证 OpenAPI 探针后，单次权威全量重跑为 1104/1104 通过、0 failure/error/skipped，详见[当前全量回归报告](../../.p8-runtime/verification/full-current-20261018/summary.json)。

## 修复结果

1. **FIXED — Upload 公共列表分页。** 接口支持范围绑定的 Base64URL `after` 游标和 `limit=1..100`，稳定键为 `upload_id`，响应固定包含 `items` 和可空 `next_cursor`。
2. **FIXED — Asset 公共列表分页。** 接口按 `asset_id + version_id` 稳定分页；前端资产目录、任务资产选择和工作流绑定共享游标模块。资产详情 URL 固定 `version_id`，旧链接会完整分页定位后改写为精确版本。
3. **FIXED — Knowledge Document 公共列表分页。** 接口按 `document_id + version_id` 稳定分页。三类游标均绑定资源类型和项目，非法、跨项目或跨接口使用返回 `400 MALFORMED_REQUEST`。
4. **FIXED — CWL 锁文件与版本漂移。** 根锁、CWL 锁和前端锁均为 `0.15.3`；构建脚本从项目元数据读取版本。CWL 使用锁文件重新构建，镜像摘要与 `0.15.2` 不同，并已通过真实 Toil 执行和仓库外安装验证。
5. **FIXED — 测试容器越界清理。** 每个 Cluster 使用唯一 `V7_TEST_RUN_ID`，科学容器附加 `v7.test-run=<id>`；清理前重新核对精确标签，不再按 `v7.phase=P4*` 清理。并行/历史容器探针证明只删除本次容器，测试结束后残留为 0。

## Embedding 失败关闭门禁

Embedding Catalog 与 Model Catalog 独立，并通过 `embedding_catalog_digest` 进入 Agent Runtime Profile 和 Release Lock 哈希链。`0.15.3` 的 Catalog 状态为 `DISABLED`，生产环境没有配置端点或凭据，Embedding 不进入 Agent 或 Knowledge 关键路径。

客户端和启用探针已覆盖 HTTPS、禁用代理继承/重定向、`0600` 凭据文件、Bearer 认证、模型/维度/量化/制品摘要一致性、响应大小、无效向量、429/5xx、15 秒硬超时、3 次预热和至少 20 次测量。历史 P6.10 证据未改写；外部端点只有在认证、TLS、制品身份和 p95 延迟全部通过后才能启用。

## 验证结果

- Python 全量：`1101 passed`，`0 failed / 0 error / 0 skipped`，耗时 2686.508 秒。
- 分页：覆盖 0、1、50、51、100、101、多版本、稳定顺序、末页、游标范围、limit 边界、RBAC、跨项目和上游错误；部署后 Chromium 探针确认第 101 条可在资产目录、任务向导和工作流绑定中访问，精确版本详情在刷新并重新输入内存令牌后仍保持定位。探针使用拦截的合成 API 响应，没有读取生产业务数据。
- 前端：TypeScript、16 项 Vitest 和生产构建通过；入口 gzip 97,738 bytes，React Flow、Monaco、JSON/YAML Worker 独立分块，生产包无 `.map`。
- P8 升级专项：20 项通过；真实 CWL 恢复/取消专项：8 项通过；仓库外发行验证覆盖 11 个 wheel、迁移、API、P3、Toil、空间与三节点地图闭环。
- Agent 黄金任务：A01–A15 连续三轮全部通过，`minimum_stability=1.0`；只使用确定性 Provider。
- Ruff 规则检查、根/CWL `uv lock --check`、Bash 语法和 Release Lock 校验通过。

## 维护切换与线上核验

维护窗口先建立写屏障并停车。停车清单中活动 Action、Execution、Agent、导入、Publication、模型调用和 Outbox 均为 0。随后生成并验证四库 schema v2 冷备份，manifest digest 为 `sha256:09a0ea48ea4bd2549c09c6e44ddb9fd411c99211e74983d38db9807df809e36e`。

`plan → prepare → apply → verify → commit` 全部完成。提交后两个入口的 health/ready 均为 HTTP 200/P8.2，Release Header 与 Release Lock 一致；16 个应用容器均使用同一应用镜像和 Release digest，重启计数为 0。OpenAPI 已公开三个新分页契约，SPA/CSP 正常，source map 返回 404，近 10 分钟无 Traceback、Schema 错误、崩溃循环或 Worker 成功日志洪泛。

旧 `0.15.2` Release、源环境快照和升级前冷备份均保留，未修改或读取线上项目业务数据。
