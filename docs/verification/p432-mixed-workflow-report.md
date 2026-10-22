# P4.3.2 / 0.5.2 混合工作流阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

版本 `0.5.2` 已把受信远程节点接入冻结工作流。已验证的业务链路为：本地 QGIS Buffer 生成私有 GeoPackage，Execution 按唯一来源和摘要把它交给远端 `spatial-vector-summary@1.0.0`，父工作流收集两个节点的核验结果后整组原子发布。

工作流包只保存项目内的 `service_binding_id + binding_revision`。Platform 在提交时解析精确修订并按完整调用路径冻结内部绑定；节点取得计算槽前重新检查绑定启用状态、授权、任务修订和预算。Execution 在同一事务创建节点执行与远程调用账本，使用节点 Action ID 作为远程幂等身份。公开执行查询和活动列表删除内部端点、凭据引用及绑定快照。

远程节点复用了 P4.3.1a 的 HTTPS、文件凭据、稳定远程身份、观察历史、流式传输和本地结果核验。派生输入仍留在 Execution 私有工作目录，摘要和来源不唯一时拒绝。父工作流取消会先停止 Toil，再持久取消仍在运行的子节点，等待子节点收敛后中止发布。

八个 Python 发行包和 CWL runtime 已统一为 `0.5.2`。本轮没有数据库迁移；Platform、Data、Execution schema 分别保持 `0006_service_binding_scope`、`0003_spatial_import_runtime`、`0006_remote_reliability`。

## 测试结果

- `bash scripts/check_p432.sh --profile standard`：在 `192.168.167.89:19080` 的真实 HTTPS peer 上，混合工作流与绑定测试 9 项通过；直接远程和 API 兼容测试 12 项通过。远端使用 `0.5.2` Native 镜像 `sha256:822586...72e1f`，结束后活动远程容器为 0。
- `bash scripts/check_p4.sh --phase cartography`：541 项 pytest 全部通过，用时 1091.19 秒；没有失败或跳过。
- P1 选型规则：13 项通过。
- 发行验证：8 个 wheel 和 sdist 构建成功；核心标准库边界、仓库外锁定安装、API、三套数据库迁移、Worker、Toil、QGIS、Native GDAL、浏览器闭环均通过。
- 代码格式与静态检查通过。

结构化证据见 [p432-mixed-workflow-evidence.json](p432-mixed-workflow-evidence.json)。

## 问题处理

本轮发现并修复四类问题：

1. 工作流编译器原先只接受本地实现，现仅为已登记的远程空间摘要开放严格端口解析。
2. 子节点创建原先不会建立远程调用账本，现与 Execution 节点记录在同一数据库事务写入。
3. Runner 原先会在节点终态报告前进入远程分支，导致远程子节点无法结算父预算；现统一先处理 `REPORTING`。
4. 发行构建脚本和独立 CWL lock 仍保留旧版本号；现已统一锁定 `0.5.2`，并用新镜像重新完成跨主机验收。

既有工作流取消测试也随新的子节点取消传播重新通过。直接远程执行、旧本地工作流和 P3/P4.2 闭环保持兼容。

## 范围说明

当前支持一个远程叶子类型：`spatial-vector-summary@1.0.0`。已证明本地节点的派生空间文件可以安全交给远程节点，并与本地成果一起原子发布。

本轮没有实现多 peer 选址、负载均衡、远程 QGIS、远程 Toil leader、跨主机高可用、用户代码或空间科学自动验收。工作流包中的服务绑定引用属于项目配置，迁移到另一项目时需要重新登记对应包。

## 阶段结论

P4.3.2 通过。真实跨主机混合工作流、预算唯一结算、远程账本、原子发布、取消兼容、完整回归和独立发行安装均通过。本轮结束，不自动进入 P5。
