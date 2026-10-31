# Capability Studio A6 / 0.14.5 项目版本库与正式执行契约

## 1. 问题与目标

A3 已能发布不可变工作流版本，但普通执行仍要求客户端提交 `PackageRef`。这会丢失用户选择的 Release 身份，也让应用客户端承担不必要的包解析责任。A6 增加项目级版本库，并允许从精确的 Definition 与 SemVer 创建正式工作流 Action。

A6 不改变 A3 的发布事实。Release 仍由一个 READY Candidate 和至少一个真实计算的 PASS Trial 支持，资格固定为 `SAMPLE_VERIFIED`。它只证明冻结样例与断言通过，不代表成果经过科学 Checker 或得到 `VERIFIED` Completion。

## 2. 权威身份

项目版本库从既有 `workflow_releases`、Definition、Revision、Candidate、Package 和 Trial 事实实时投影，不复制记录，也不引入 latest 指针。每项返回：

- Release、Definition、项目和稳定键；
- 规范 SemVer、历史 metadata、source 与 source digest；
- 精确 PackageRef、Candidate、Revision、context digest 与 Trial；
- `SAMPLE_VERIFIED` 资格；
- 当前是否可执行及结构化原因。

`current_executable` 是当前部署下的准入提示。历史身份始终可读；环境、包、算子实现或远程绑定不可用时，不修改历史 Release，只阻止首次新执行。

## 3. 正式执行

公开接口为：

```text
GET  /api/v1/projects/{project_id}/workflow-releases
POST /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases/{version_label}/executions
```

POST 请求只接受 Task、预期 TaskSpec revision、工作流输入、最大节点执行数及可选研究上下文。客户端不能提供 PackageRef、算子实现、镜像、命令、资源档或 Release 资格。

服务端按以下顺序处理首次请求：

1. 检查当前操作者在项目中的 EDITOR 权限。
2. 精确读取 Definition 与 SemVer 对应的 Release。
3. 核对当前环境、Package、Operator Catalog 和远程绑定可执行性。
4. 从 Release 取得精确 PackageRef，并按冻结输入 Schema 通过 Data 解析 File 输入。
5. 复用现有工作流准入，原子写入预算预留、Action、Command、outbox 和父工作流额度。
6. 在 Action 请求中冻结 `_workflow_release`，包括 Release、Definition、稳定键、SemVer、Candidate、Revision、source、Package 和 context 身份。

最终事务重新读取 Release 并逐项比较冻结身份，防止解析输入和构建执行图期间发生版本竞争。授权仍要求 Task grant 同时覆盖精确 Workflow Package、叶子算子与输入资产。

## 4. 幂等、恢复和执行语义

幂等作用域为现有操作者、项目、操作和请求键。请求摘要包含 Definition、SemVer、Task、输入、节点额度及研究上下文。

- 同键同内容先返回原 Command/Action，不重新访问 Data、重新编译或重复预留。
- 重放仍重新检查当前身份与项目权限。
- 同键不同内容返回 `409 IDEMPOTENCY_CONFLICT`。
- 首次提交后 Release 当前变为不可用，不改变原 Action；Worker 按 Action 已冻结的 Package、计划和恢复材料继续现有保守恢复协议。
- Action、Execution、节点、预算、JobStore、原子发布和取消语义与普通工作流完全相同。

Release 身份只增加来源精度，不创建第二套调度状态机，也不改变科学验收。成果若要形成 VERIFIED Completion，仍必须登记精确 Deliverable 并通过 CoveragePlan 与受信 Checker。

## 5. 页面与边界

Studio 提供项目版本库和“从版本库运行”面板。页面只提交 Task、输入和节点额度；刷新后从 API 重建版本库，令牌仍只保存在内存。页面明确区分 `SAMPLE_VERIFIED` 与科学 `VERIFIED`。

A6 不包含：

- 公共市场、跨项目共享或 latest 别名；
- Release 覆盖、删除或隐式升级；
- Agent 编写或选择工作流；
- 用户镜像、命令、脚本或任意表达式；
- 新的工作流语义、数据库表或迁移。

Platform schema 保持 `0022_operator_proposals`。A6 只增加查询、准入用例和 Action 中的冻结来源快照，旧 Action 与 Release 不重写。

## 6. 验收

必须证明项目隔离、稳定分页、客户端 target 注入拒绝、精确身份冻结、授权失败无部分预算/Action、重放早于外部依赖、HTTP/OpenAPI、真实浏览器展示，以及 A1–A5 的真实 PostgreSQL、Docker、Toil、QGIS 和制图闭环保持兼容。

统一入口：

```bash
bash scripts/checks/check_authoring.sh --phase release-library
bash scripts/checks/check_p7.sh --phase catalog
bash scripts/checks/check_p7.sh --phase compatibility
uv run --locked python scripts/checks/verify_distribution.py
```
