# P4.3.1a / 0.5.1 远端可靠性补齐报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 新增 `v7-remote-execution-v2`：强制 HTTPS、指定 CA 校验、目标实现摘要和扩展观察事实；v1 只用于恢复旧 Action。
- ServiceBinding 改为项目复合身份，管理入口和业务入口共用标准库严格校验。Platform schema 升至 `0006_service_binding_scope`。
- Execution 增加只追加远端观察历史、事件冲突检查、文件凭据、流式限量下载及严格空间结果比对。Execution schema 升至 `0006_remote_reliability`。
- 远端 peer 使用持久 SQLite 状态机，每个 Action 对应一个稳定 Docker 容器。容器固定 1 CPU、1 GiB、128 PIDs、无网络、只读根目录、能力全删和 `no-new-privileges`。
- 取消先落账再处理容器；peer 重启、Docker 暂时不可查询和响应未知时继续定位原身份，不把未知当作未执行。
- 输入和输出采用分块摘要、临时文件、`fsync` 和原子改名；远端任务容器不获得 Docker socket、数据库凭据或用户 Token。
- 八个 Python 包和 CWL runtime 统一为 `0.5.1`。Data schema 保持 `0003_spatial_import_runtime`。

## 真实远端结果

验收端为 `192.168.167.89`，远端 Docker 28.1.1。通信使用含 IP SAN 的自签 CA 证书并执行完整证书校验。固定 Native 任务镜像为：

```text
sha256:7698ebc29b4f763d057c067dc7a2d1ad280a34344617d745a6225cc9f5b999cb
```

真实 GeoPackage 闭环完成上传、远端计算、流式回收、本地事实核验、Data 原子发布和唯一预算结算。Docker inspect 证明任务容器满足上述隔离限制，退出后 PID 为 0。

额外故障探针结果：

- 连续 5 次排队后立即取消全部形成 `CANCELLED, executed=false`，没有任务容器偷跑。
- 运行中的容器取消形成 `CANCELLED, executed=true, cleanup_state=CONFIRMED`。
- 在远端观察为 `RUNNING` 时强制终止 peer，再以同一账本启动；终态为 `SUCCEEDED`，前后均只有一个容器且 remote execution ID 未改变。
- 错误 Token 返回 401，未安装指定 CA 时 TLS 握手失败；两个 Action 并发运行时保持不同 Action、execution 和容器身份，均成功收敛。
- 首次跨机验收发现远端 Python 不支持字典合并语法。计算容器已经完成，但 peer 保守停在 `COLLECTING`；修复兼容写法并重启后，原 Action 和原容器成功收敛，没有重算。

## 测试结果

```text
bash scripts/check_p43_reliability.sh --profile standard
  23 passed：状态机、凭据、v2 契约、迁移、流式上限和观察恢复
  12 passed：真实 HTTPS 远端闭环及 API 回归

bash scripts/check_p43.sh --phase direct
  208 passed

bash scripts/check_p4.sh --phase cartography
  535 passed，13 项 P1 规则通过
  8 个 wheel/sdist、仓库外安装、API、迁移、Worker、Toil、QGIS 和 Data 发布验证通过
```

制图统一门禁的 535 项功能与发行测试全部通过后，旧 P4.2.2 证据记录器因把包版本硬编码为 `0.4.1` 而退出。记录器已改为同时保存阶段基线和当前运行版本，并在现有 JUnit 与发行日志上单独复验通过；没有重复运行已经通过的 17 分钟容器套件。

结构化证据见 [p431a-reliability-evidence.json](p431a-reliability-evidence.json)。原 [0.5.0 报告](p43-remote-report.md)保留为首次验收历史。

## 已修复问题

- 修复排队取消与领取竞争，取消后不能创建或启动任务容器。
- 修复 Docker daemon 暂时不可用被误判为“容器不存在”的风险；瞬时错误在原 Action 上重试。
- 修复 peer Worker 遇到瞬时 Docker/SQLite 故障后停止对账的问题，并让查询重新唤醒未终态 Action。
- 修复远端 Python 3.8 兼容问题；部署基线明确为 Python 3.8 以上。
- 修复大文件摘要路径整文件读入内存的问题。
- 修复外部远端测试错误地在本机 Docker 查找远端容器的问题；宿主隔离由独立远端管理探针核验。
- 修复后续版本运行 P3/P4 历史证据记录时的版本语义，区分阶段基线与当前兼容运行版本。

## 范围和限制

当前只支持一个登记 peer 上的直接 `spatial-vector-summary@1.0.0`。它是单实例持久服务，不具备高可用或自动故障转移；远程 CWL 节点、多个远端选择、混合本地/远程工作流和 mTLS 尚未实现。Bearer Token 与 TLS 服务端身份分工明确，Token 轮换仍由运维管理。

本轮验证逻辑正确性与恢复，不建立吞吐量结论。OOM、PID 上限和输出超限由容器及代码限制覆盖并有单元/契约检查，尚未在真实远端逐项执行破坏性资源耗尽实验。

## 阶段结论

P4.3.1a 的直接远程可靠性闭环通过，版本为 `0.5.1`。本轮结束，不自动进入远程工作流、P5 Knowledge 或 Agent。
