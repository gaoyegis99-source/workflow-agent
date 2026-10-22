# Capability Studio A7 / 0.14.6 部署、升级与回退

## 升级

1. 等待正在写入 Release、Action 和 Publication 的请求收敛。
2. 停止 Platform API、Platform Worker、Execution Worker、Authoring Worker 和 Agent Worker。
3. 备份 Platform 数据库。
4. 安装全部 `0.14.6` 发行物、匹配的 CWL runtime 和 Studio 静态文件。
5. 显式迁移 Platform 到 `0023_workflow_release_lifecycle`；Data、Execution、Knowledge schema 不变。
6. 核对三个 Catalog、七个镜像、11 个 wheel 和前端资源与 A7 Release Lock 一致。
7. 启动服务，运行 A7 定向门禁、A1–A7 Authoring 回归和兼容回归。

迁移为旧 Release 回填 ACTIVE 初始事件，并增加发布许可表。服务启动仍不自动迁移，不允许新旧 Platform 进程混跑。

## 运行语义

- 临时停止新提交使用 `DISABLED`。恢复时追加 `ACTIVE` 修订。
- 永久阻止新提交、新节点和首次发布许可使用 `REVOKED`。该状态不可恢复。
- 撤销不会自动取消已经运行的计算；需要停止时另外提交取消请求。
- 发布许可先于撤销取得时，允许该既有 Action 完成发布。可从 `workflow_release_publication_permits` 和生命周期历史审计先后顺序。
- 已经提交到 Data 的资产不会因版本停用或撤销而删除。

## 回退

`0.14.5` 不理解生命周期修订和持久发布许可。若 `0023` 已产生任何非初始事件或发布许可，只能停止服务并恢复升级前数据库备份，不能删除审计事实强行降级。

仅在确认没有新增生命周期事件和发布许可时，才可停止所有进程、执行 Alembic 降级到 `0022_operator_proposals`、安装 `0.14.5` 并恢复 A6 Studio。回退不得修改 Release、Trial、Action、预算、JobStore、Publication、资产或 Completion。
