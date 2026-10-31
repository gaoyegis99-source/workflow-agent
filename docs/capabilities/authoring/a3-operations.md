# A3 / 0.14.2 部署与回退

升级顺序：

1. 等待正在发布的成果收敛，停止 Platform API、platform-worker、Execution Worker、Agent Worker 和
   `platform-authoring-worker`。
2. 安装完整 `0.14.2` wheel 集、CWL runtime 与 Capability Studio 静态资源。
3. 显式将 Platform 数据库迁移到 `0021_authoring_release`；Data、Execution 和 Knowledge 不迁移。
4. 核对三个目录、七个镜像、11 个 wheel、前端资源和 release lock。
5. 启动服务与 Worker，运行 `bash scripts/checks/check_authoring.sh --phase workflow-release`。

`platform-authoring-worker` 需要原有 `V7_DATABASE_URL`、`V7_CWL_IMAGE`，以及 Trial 判定所需的
`V7_DATA_URL`、`V7_EXECUTION_URL` 和对应服务令牌。缺少后两项时 Worker 仍可处理 A2 Validation，
但不会领取 Trial；生产 A3 部署必须配置完整。

升级后不要混跑 0.14.1 与 0.14.2 的 API/Worker。回退到 0.14.1 前必须让在途 Trial 收敛。若 0021
表中已经产生 Candidate、Trial 或 Release，只能停止服务并恢复升级前数据库备份，不能删除这些历史来
强制降级。
