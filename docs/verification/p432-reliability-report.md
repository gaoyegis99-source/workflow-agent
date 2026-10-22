# P4.3.2 补强 / 0.5.3 混合工作流可靠性报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

版本 `0.5.3` 收口了本地 QGIS Buffer 与远程 Spatial Summary 混合工作流的成果隔离、身份恢复、绑定竞争和取消仲裁。

- 远程文件先进入 `remote-incoming/<remote_revision>/`；端口、媒体类型、大小和 SHA-256 全部通过后，才以原子改名提升为节点私有输出。下载中断、临时 404、错摘要和缺端口不能进入 gather。
- 父工作流每次进入或恢复发布时重新核对成功子节点、私有状态、Action/Execution、端口、摘要、来源路径和最新取消事实。Data 中止先完成时不产生资产；Data 已提交时父级保持成功并记录 `TOO_LATE_COMMITTED`。
- 远程子节点与 `remote_calls` 在同一 PostgreSQL 事务创建；现有节点缺账本时返回 `INVALID_BINDING`，不临时修补或发起远程请求。
- 远程节点在输入上传前和 submit 前各申请一次许可。绑定禁用会阻止尚未提交的调用；已经接受的调用继续使用冻结修订、端点和远程身份。
- 普通 404、连接失败和响应丢失保持未知。额度已经开始执行时保留为消耗，尚未确认开始时保留为预留；只有受信的未执行拒绝或取消释放预留。
- 公开执行查询增加稳定的远程失败分类；本轮没有新增公开请求字段、路由、状态码或数据库迁移。

八个 Python 发行包和 CWL runtime 已统一为 `0.5.3`。Platform、Data、Execution schema 分别保持 `0006_service_binding_scope`、`0003_spatial_import_runtime`、`0006_remote_reliability`。

## 问题处理

实施与验收中发现并修复了四类实际缺陷：

1. 远程节点在第一次许可未获得计算槽时仍进入 REPORTING，Platform 会拒绝这个无槽节点观察并造成永久重试。现在无槽拒绝直接收敛为本地 `REJECTED/FINAL`。
2. 工作流发布校验异常曾走通用 Runner 中止路径，可能丢失提交/中止竞争语义。现在所有工作流发布异常都回到工作流专用仲裁，并在 Data prepare 前再次读取取消事实。
3. 远程 Execution 已存在但账本缺失时，旧逻辑可能补建账本。现在创建事务返回前强制核对两条记录；缺失或摘要不一致直接拒绝。
4. 跨主机取消测试曾假设未知期必须仍显示为 `reserved`。真实 peer 可能已经开始计算并转为 `used`；验收改为检查 `used + reserved` 不丢失，同时分别验证未执行拒绝会释放预留。

测试专用 HTTPS 代理和 Data 代理只存在于 `tests/p432`，没有加入生产故障开关或任意状态写入接口。

## 测试结果

- `bash scripts/check_p432_reliability.sh --profile standard`：`25 passed`，`0 failed`，`0 skipped`，465.55 秒。远端为 `192.168.167.89:19080` 的真实 HTTPS peer；测试通过受限 SSH 命令重启该专用 peer，并保留原 SQLite、工作目录和实例身份。
- `bash scripts/check_p43.sh --phase direct`：`210 passed`，111.80 秒，直接远程、契约、迁移及兼容回归通过。
- `bash scripts/check_p4.sh --phase workflows`：`564 passed`，`0 failed`，`0 skipped`，1521.12 秒；P1 规则 `13/13` 通过；8 个 wheel/sdist、仓库外安装、真实 HTTP、迁移、Worker、容器、P3 科学闭环、P4.1/P4.2/P4.3 回归全部通过。
- 0.5.3 空间镜像重建后定向复测：`6 passed`，198.67 秒，覆盖 QGIS→Native、Buffer→Summary→Map、混合远程成功以及明确拒绝、错摘要、缺端口。
- 0.5.3 空间镜像重建后的真实 Chromium 页面回归：`2 passed`，84.51 秒，覆盖空间任务页和静态地图预览/下载。

固定镜像：

- QGIS：`sha256:533f6e7b2560674fd4738756636929b83732807d7b013938ad915d35ff21e534`
- Native/远端 Native：`sha256:7a92c031ce4a8960b171860b759029a9e081751c1af64b64e1003999277095c0`
- CWL：`sha256:652754092f5cf48486a9c8bb42f113d1d95d25141ef7d846be44ff8a18504a09`

standard 结束后，远端非终态 Action 为 0、带 `remote051` 标签的运行任务容器为 0；peer 健康探针返回受信的 404 未找到响应。凭据、Token 和私钥没有写入仓库、日志或结构化证据。

## 支持范围与限制

当前支持直接远程空间摘要，以及冻结工作流中的本地 Buffer → 远程 Summary → 本地整组发布。支持响应丢失、临时 404、输出断流、本地 Worker/Toil/peer 组合重启、绑定禁用和修订冻结。

本轮没有增加远程算子、多远端调度、跨主机高可用、性能优化、空间科学验收、Knowledge 或 Agent。远端 peer 仍是受信宿主服务，并需要 Docker 管理权限；这项部署边界未改变。

## 阶段结论

P4.3.2 补强通过。4 项 L0 与 3 项 L1 的必测语义均有生产实现和确定性回归证据；失败、取消、重放和恢复不会公开半组成果，不会更换远程身份，也不会重复计算、扣账或发布。本轮结束，不进入 P5。
