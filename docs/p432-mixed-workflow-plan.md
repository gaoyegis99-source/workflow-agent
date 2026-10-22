# P4.3.2 / 0.5.2 远程节点进入冻结工作流

本轮把已验收的远程 `spatial-vector-summary@1.0.0` 接入现有冻结工作流。最小闭环为：本地 QGIS Buffer 产生私有 GeoPackage，远程 Summary 按摘要接收该派生输入，父工作流最后原子发布整组成果。

实现边界：工作流包只保存 `service_binding_id + binding_revision`，不保存端点、凭据或 CA；提交时由 Platform 解析并冻结服务绑定；每个远程节点启动许可前重新核验项目、修订、启用状态、授权及预算；Execution 用稳定节点 Action 建立唯一远程调用账本。Toil、节点身份、预算计次和原子发布协议保持不变。

验收顺序：契约与编译测试、服务绑定冻结与撤销测试、本地派生文件到远程节点的真实 HTTPS 测试、取消和恢复测试、P4.3.1a/P4.2/P4.1/P3 兼容回归。P4.3.2 不增加多远端调度、远程 QGIS、远程 Toil leader 或任意用户代码。
