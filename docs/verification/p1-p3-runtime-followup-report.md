# P1–P3 运行时依赖与组件复验

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

本轮针对 `0.2.1` 工作区处理 Starlette/AnyIO 弃用警告，并重新执行 P1 重型组件实验。历史 [P1 证据](p1-evidence.json)、[首次复验证据](p1-followup-evidence.json)及 [P3 首次补齐证据](p3-followup-evidence.json)保留；rootless 探针修正后的运行见 [最新 P1 证据](p1-rootless-correction-evidence.json)。未修改 V6，未进入 P4。

## 处理结果

| 项目 | 结果与边界 |
| --- | --- |
| Starlette/AnyIO 警告 | 将锁文件中的 Starlette 从 `0.52.1` 升至 `1.7.0`，开发测试安装 `httpx2`，不屏蔽警告；增加将 `DeprecationWarning` 视为错误的导入回归测试。原 AnyIO 弃用别名不再被测试客户端导入。 |
| Toil 9.5.0 | 真实提交、步骤定位、原 JobStore 恢复、取消均通过，仍为按 P1 规则选定的 CWL 候选。 |
| Weaver 6.16.0 | 提交、scatter、API 重启定位通过；`DELETE` 使 Job 变为 `dismissed` 后，实际 Docker 工具在 15 秒窗口仍运行，因此取消门槛仍失败，不能列为合格候选。 |
| gVisor | 专用包装配置的文件、网络、临时目录和实际取消检查通过。最初的直接 OCI 探针混用了内置 `--rootless` 与 OCI user namespace 映射；去掉该标志后遇到当前用户无法创建 memory cgroup。采用 `--ignore-cgroups` 后，原生 rootless OCI 的启动、状态查询、取消通过，但资源限额没有验证，不能代替专用包装配置。 |

固定的 Weaver 6.16.0 镜像中，`dismiss_job_task` 对运行中 Job 发送 `celery_app.control.revoke(job.task_id, terminate=True)`，随后立即把数据库状态写为 `dismissed`；它没有确认 Docker 工具容器停止。实际探针证明工具仍运行。Celery 的 [revoke 文档](https://docs.celeryq.dev/en/stable/userguide/workers.html)也明确区分终止执行任务的 Worker 子进程与外部执行。要使 Weaver 合格，需要按稳定 Job/工具容器身份发取消、核对容器确已停止、对未知结果继续查询，并在整个过程禁止迟到结果发布；不能仅接受 dismissed 回执。

gVisor 的 [官方 rootless 文档](https://gvisor.dev/docs/user_guide/rootless/)区分内置 `--rootless do` 和 OCI user namespace 映射。旧探针将两者混用，最初的 gofer `operation not permitted` 不能推断原生 rootless OCI 不可用。修正后它可以运行与取消；但当前主机对所需 memory cgroup 返回 `permission denied`。`--ignore-cgroups` 仅用于诊断，不可作为生产资源限额方案。若要求完全 rootless 部署，仍需可委派的 cgroup 配置并实测 CPU、内存、PID 限额，以及文件和网络边界。

宿主诊断显示 cgroup v1 的 `memory`、`cpu` 控制器与 v2 `unified` 并存，当前进程所在的 v1 memory 路径归 root 所有；使用 `--systemd-cgroup` 的一次性诊断仍在该路径收到 `permission denied`。因此不能只给现有命令添加该标志就声称限额可用。若准备专用 rootless 主机，需先完成统一 cgroup v2 与运行用户委派，再在不使用 `--ignore-cgroups` 的条件下触发式验证限额。该宿主调整尚未实施。

P1 最新复验命令为 `bash scripts/check_p1.sh --record docs/verification/p1-rootless-correction-evidence.json`，退出码 0。150 项 P0 测试、13 项 P1 规则、8 个发行包仓库外安装、Toil 与专用包装 gVisor 重型探针均通过；原生 rootless `do` 和忽略 cgroup 的 OCI 启动／取消诊断通过；Weaver 的实际取消门槛仍失败。P1 按显式规则选择 Toil。首次复验中的 OCI 失败结论由本次诊断修正；两个证据文件均保留，便于核查原因和差异。

P3 统一入口 `bash scripts/check_p3.sh` 退出码 0：[本轮证据](p3-runtime-followup-evidence.json)记录 **317 passed、0 failed、0 errors、0 skipped**，13 项 P1 规则通过，8 个 wheel 和 sdist 仓库外安装及真实 API/CLI/Worker/容器闭环通过。单独以 `-W error::DeprecationWarning` 导入 `fastapi.testclient` 成功，完整 pytest 输出没有弃用警告。Ruff 检查与格式检查通过。此次 rootless 探针修正只涉及 P1 对照实验，不改变生产 P3 运行路径；P3 的上述回归是在修正前完成的。
