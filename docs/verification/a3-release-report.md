# Capability Studio A3 / 0.14.2 阶段报告

> HISTORICAL. A3 / 0.14.2 阶段验收记录；当前活动 Release 以机器可读 pointer 为准。

## 完成内容

- 新增 Platform `0021_authoring_release`，持久保存 Candidate、真实 Trial、不可变 Release 及有序 Trial 证据。A1/A2 Definition、Revision、Validation、既有 WorkflowPackage、Action 和成果均不改写。
- Candidate 只冻结当前有效的 PASSED Validation。服务端在数据库事务外确定性编译 expected manifest，在最终事务中重新核对 Revision、Validation、目录和环境，并原子写入 WorkflowPackage、Candidate 与幂等结果。
- `PackageProcessor` 对 Studio Candidate 使用冻结 source 重新编译，完整比较 expected manifest，并在固定 cwltool 容器中验证。一致时才把原 Package 标为 READY；不一致稳定拒绝，不生成替代包。
- 工作流准入已拆为准备与提交两个内部阶段。Trial、父 Action、Command、预算 envelope 和 outbox 在同一事务提交；Task、grant、预算、目录、远程绑定和 Candidate 在提交时重新核对。
- 服务端生成 `trial-case-v1`，冻结 Candidate、PackageRef、解析输入、全部计算节点、必要最终输出、受信 Schema 断言、ExecutionPlan 和断言实现摘要。客户端不能删减 required steps/outputs 或写 verdict。
- `platform-authoring-worker` 公平轮询 Validation 与 Trial。Trial 判定只读取原 Action、Execution、节点和 Data 原子 publication；不创建 Action、不运行容器、不增加预算。租约、generation 和不可变 PASS/FAIL 阻止旧 Worker 覆盖。
- 临时 404、连接失败和依赖不可达保持原 Trial/Action 并进入可重试 ERROR；恢复后查询原身份。身份或协议冲突形成不可自动重试 ERROR。成功 Execution 但样例断言失败时，已发布成果和执行事实保持不变。
- Release 使用规范 SemVer，无 `latest` 指针。发布重新核对 READY Package、环境、算子生命周期、远程绑定、Trial/Execution/publication 和 case/result 摘要；不重新编译、执行或复制成果。
- Capability Studio 新增 Candidate、真实 Trial、Release 三个面板。页面使用已有 DIRECT Task、grant 和预算，刷新后从 API 重建状态，Token 不进入 URL 或浏览器持久存储。
- 两条真实浏览器业务闭环已交付：
  - `Filter → Aggregate → Profile`：浏览器提交 Trial，3 个节点分别计费并整组发布；随后登记精确 `WORKFLOW_OUTPUT` Deliverable，运行 runtime-integrity 与独立 profile reproduction Checker，Task 科学验收 PASS。
  - `Buffer → Summary / StaticMap`：浏览器提交 Trial，真实 QGIS、Native 和 PNG 三节点整组发布，再由浏览器发布不可变工作流版本。
- 11 个 Python wheel、CWL runtime 元数据和 Studio 统一为 `0.14.2`。Release lock 固定 11 个 wheel、3 个 Catalog、7 个镜像和 3 个前端资源。

## 接口与数据库

新增公开接口：

- `POST/GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/candidates`
- `GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/candidates/{candidate_id}`
- `POST/GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/trial-runs`
- `GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/trial-runs/{trial_id}`
- `POST/GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases`
- `GET /api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases/{version_label}`

Candidate 和 Trial 创建返回 202，Release 创建返回 201。全部写入要求 `Idempotency-Key`，重放仍检查当前身份和项目权限。Platform schema 为 `0021_authoring_release`；Data、Execution 和 Knowledge schema 未改变。

## 测试结果

| 门禁 | 最终结果 |
| --- | --- |
| `uv run --locked pytest -q tests/authoring/a3` | 17 passed；包含迁移、事务故障、幂等、generation、临时 404、HTTP、浏览器恢复及两条真实容器工作流 |
| `bash scripts/check_authoring.sh --phase workflow-release` | 52 passed；A1+A2+A3、前端 typecheck、4 项 Vitest、生产构建、Ruff 和 655 个文件格式检查全部通过 |
| 表格真实浏览器闭环 | PASS；3 次计算 + 2 次独立检查，最终预算 `used=5, reserved=0, in_flight=0`，Release 为 `SAMPLE_VERIFIED`，Acceptance 为科学 PASS |
| QGIS 真实浏览器闭环 | PASS；Buffer、Summary、StaticMap 各执行一次，3 项成果整组原子发布并形成 Release |
| `bash scripts/check_p7.sh --phase catalog` | 24 项目录测试和 13 项 P1 规则通过；Ruff/格式通过 |
| `bash scripts/check_p7.sh --phase compatibility` | 最终 919 passed、0 failed、0 skipped，2363.83 秒 |
| `uv run --locked python scripts/verify_distribution.py` | PASS；仓库外构建/安装、API、迁移、CLI/Worker、P3、P4 Toil、空间和制图真实容器路径全部通过 |

首次完整兼容运行结果为 917 passed、2 failed。一个失败是 OpenAPI 精确集合尚未列入 A3 的 6 条正式路由；另一个是 P4 浏览器按钮在 40 分钟整套运行中的一次时序抖动。路由断言已更新，P4 浏览器场景定向复验 2/2 通过；随后完整兼容门禁重新执行并得到 919/919 通过。没有把定向复验代替最终完整回归。

## 问题处理

- 修复 Trial admission 内部参数遮蔽，防止 authoring operation 名称被嵌套字段替换并破坏幂等作用域。
- 修复临时资产 404 被样例判定吞掉并永久记为 FAIL 的问题。现在通信不确定性保留原身份和预算事实，恢复后由新 generation 继续判定。
- Candidate 插入失败、Trial 插入失败和授权失败均有事务回滚测试，证明不会遗留孤立 Package、Action、Command、预算 envelope 或 outbox。
- Release 查询在语义环境暂不可用时返回 `current_executable=false` 和明确原因，不触发响应 500，也不改写历史 Release。
- Studio 新增第二个下拉框后，旧 A1 浏览器测试的宽泛 `select` 定位变得多义。现已给算子选择器增加明确可访问标签，旧页面和新 A3 页面均通过浏览器测试。
- Vite 仍报告单个 JS chunk 约 666 KiB。A3 没有前端性能门槛，该警告不影响逻辑或安全验收；后续页面继续扩大时应按编辑器和发布面板拆包。

## 范围说明

A3 支持至少一个直接计算节点的基础工作流。Trial 正常公开成果并计费；样例 PASS 只证明冻结输入满足冻结断言。科学完成仍由 Deliverable、Checker 和 Acceptance 判断，页面分别展示这两类事实。

A3 未加入条件、scatter、子工作流、源码导入、任意表达式、自动 Task/grant/预算创建、A4 注册市场或 A5 高级图路由。OpenAPI 和数据库均没有这些占位接口或表。

## 阶段结论

A3 通过，发行版本为 `0.14.2`，Platform schema 为 `0021_authoring_release`。真实浏览器、PostgreSQL、HTTP、Docker、Toil、QGIS、Native、独立 Checker、仓库外安装和完整兼容回归均通过。阶段结束，不自动进入 A4/A5。
