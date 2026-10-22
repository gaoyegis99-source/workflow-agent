# Capability Studio A3 / 0.14.2 候选、试运行与版本契约

## 1. 权威对象

A3 将编辑结果分成三个不可混用的事实层：

- `WorkflowCandidate` 冻结 Revision、PASSED Validation、编译清单、语义环境和唯一
  WorkflowPackage。Package 的处理状态通过查询投影，不复制状态。
- `WorkflowTrial` 在已有 DIRECT Task 的授权和预算内创建真实 Workflow Action。Trial 与预算预留、
  Command、outbox 和父 Action 在同一事务写入。
- `WorkflowRelease` 绑定一个 READY Candidate 和一组不可变的 `SUCCEEDED + PASS` Trial。版本号是
  无 build metadata 的规范 SemVer，不存在 `latest` 指针。

样例结论 `PASS` 的含义限于：冻结输入的真实执行成功、所有冻结计算节点实际成功、Data 已完成整组原子
发布、必要最终输出的类型/形状存在，且调用者选择的受信 Schema 和可选表示摘要匹配。它不替代
Deliverable、Checker 或 Acceptance，也不会把 Task 标记为科学 VERIFIED。

## 2. 冻结和准入

候选只接受当前有效的 PASSED Validation。A3 源码必须只有一个根 Workflow，至少有一个直接计算
步骤；条件、scatter、子工作流和运行时源码导入均被拒绝。服务在事务外确定性编译 expected manifest，
然后在一个事务内重新核对 Revision、Validation 和环境并写入 Package、Candidate 和幂等结果。

PackageProcessor 使用候选冻结的源码和环境重新编译，比较完整 manifest，再调用固定 cwltool 镜像。
摘要或环境不一致会使包稳定进入 REJECTED，不会切换包身份或静默回退。

试运行先按精确 Package 解析 Data 输入并生成 ExecutionPlan/EnginePackage，随后锁定 Task 并重新核对
授权、预算、目录、远程绑定和 Candidate。在同一事务内写入 Trial、Action、Command、预算 envelope
和 outbox。幂等重放在 Data 查询和图展开前完成，并重新检查当前项目访问权限。

## 3. 判定和发布

`platform-authoring-worker` 公平处理 Validation 与 Trial。Trial 判定使用租约和递增 generation，且只读取
既有 Action、Execution、节点和 Data publication：

- 非终态执行保持 PENDING。
- 失败、取消、必要节点未实际成功、必要输出缺失或样例断言不符形成不可变 FAIL。
- 临时 404、连接失败或依赖不可达形成可重试 ERROR；身份/协议冲突形成不可自动重试 ERROR。
- PASS/FAIL 不能覆盖，旧 generation 不能回写。

判定不创建 Action、不运行容器、不消耗预算。成功执行但断言失败时，原 Execution 和已经原子发布的
资产保持原事实。

Release 创建重新核对 Candidate、Package、Trial case/result 摘要和当前语义环境，并在同一事务写入
Release 与有序 Trial 证据。响应丢失使用原 Idempotency-Key 返回同一版本。查询同时返回历史精确
source、PackageRef、trial 证据摘要和当前可执行性；历史身份不会被当前目录或绑定替换。

## 4. HTTP 接口

令 `P=/api/v1/projects/{project_id}`：

- `POST/GET P/workflow-definitions/{definition_id}/candidates`
- `GET P/workflow-definitions/{definition_id}/candidates/{candidate_id}`
- `POST/GET P/workflow-definitions/{definition_id}/trial-runs`
- `GET P/workflow-definitions/{definition_id}/trial-runs/{trial_id}`
- `POST/GET P/workflow-definitions/{definition_id}/releases`
- `GET P/workflow-definitions/{definition_id}/releases/{version_label}`

所有写入要求 `Idempotency-Key`。Candidate 和 Trial 创建返回 202；Release 返回 201。列表分别按稳定
Candidate ID、Trial ID 和 SemVer 排序分页。

## 5. 明确边界

A3 不创建 Task、grant 或额度，不提供条件、scatter、子工作流、任意表达式、工作流导入、市场或
`latest` 别名。Trial 成果按正常 Data 协议公开并计费；页面分别展示执行、节点、发布、取消、预算和
样例判定，不把 UNKNOWN 显示成停止或失败。
