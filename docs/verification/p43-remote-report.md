# P4.3.1 / 0.5.0 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- Platform 增加不可变 ServiceBinding 修订、独立启停状态、管理 CLI 和脱敏发现接口。
- Execution 增加 `remote_calls` 账本、CredentialProvider、无重定向 HTTP 客户端、稳定 Action 提交/查询/取消、单调观察校验和受信输出回收。
- 远端只执行固定 `spatial-vector-summary@1.0.0`；Data 在本地完成最终原子发布。
- 参考远端服务使用持久状态和固定 Native GDAL 镜像。真实部署在 `192.168.167.89`，通过 Docker 28.1.1 运行。

## 测试结果

`V7_P43_REMOTE_URL=http://192.168.167.89:19080 V7_P43_REMOTE_TOKEN_FILE=.p4-runtime/remote/token bash scripts/check_p43.sh --phase direct`：197 passed，0 failed，0 skipped；P1 规则 13 项通过。真实闭环完成 GeoPackage 上传、远端执行、结果校验、本地发布和唯一预算结算。

远端改用全新持久卷后再次运行真实端到端用例：1 passed。远端容器状态为 `running`，镜像为 `sha256:8b8d6a964ff500ba2429bbf187d3fb7face297607c7e31bdb0448c9b6a2574d3`，重启策略为 `unless-stopped`。八个发行包的仓库外构建、安装、迁移、API、CLI、Worker、P3、P4.1 和 P4.2 安装态验证通过；源码与文档凭据扫描结果为 0 个泄漏文件。

额外全仓非性能探针在 10 分钟时完成 333 项并发现 OpenAPI 路由期望未更新；修复后由统一 P4.3 入口回归该测试。按本轮“逻辑优先、不做效率优化”的范围，没有重跑 P4.1 长节点性能矩阵。

## 问题处理

- 修复远端接受记录提交前启动 Worker 的竞争，改为持久提交后启动。
- 修复 Data 直接空间发布把 manifest 摘要变量覆盖为输出集合，导致 PostgreSQL 不能写入的问题。
- 保持 404、网络故障和响应丢失为未知状态，不重复提交、不提前释放预算。

## 范围与结论

当前支持直接远程空间摘要和故障恢复，不支持把远程节点混入 CWL 工作流，也不宣称跨主机高可用。P4.3.1 通过并在本轮结束；未进入 P4.3.2 或 P5。
