# Capability Studio A6 / 0.14.5 部署、升级与回退

## 升级

1. 等待活动 Trial、工作流引擎、Publication 和 Acceptance 收敛。
2. 停止 Platform API、Platform/Authoring/Execution/Agent Worker。
3. 安装完整 `0.14.5` 发行物和匹配的 CWL runtime、Studio 静态文件。
4. 显式运行 Platform 迁移命令并核对 schema 仍为 `0022_operator_proposals`；其他数据库版本不变。
5. 核对 11 个 wheel、三个 Catalog、七个镜像和 Studio 静态清单与 A6 Release Lock 一致。
6. 启动服务，运行 A6、目录、兼容和独立发行门禁。

A6 没有数据库迁移，启动仍不自动迁移。停止服务是为了避免新旧 API 对同一 Release 产生不同的 Action 来源快照。

## 运行

- `GET /workflow-releases` 是项目内不可变版本库；没有跨项目发现。
- 正式执行必须选择精确 Definition 与 SemVer，并使用已有 DIRECT Task、grant 和预算。
- 页面显示不可执行原因；历史 Release、Trial 和成果仍可查询。
- `SAMPLE_VERIFIED` 不能作为科学验证或 Task Completion 的替代。
- 响应丢失使用原 Idempotency-Key 重放；不得换键重新提交。

## 回退

回退到 `0.14.4` 前先让 A6 正式工作流执行和发布收敛并停止写入。数据库无需降级；`0.14.4` 可以继续读取底层 Package、Action 和成果，但不理解 Action 中新增的 `_workflow_release` 来源，也不提供项目版本库执行入口。

不得删除或改写 Release、Action、预算、JobStore、Publication、成果和 Completion。活动 A6 执行应优先以前向修复方式由 `0.14.5` 恢复，不能让旧 Worker 用另一 Package 或身份重新运行。
