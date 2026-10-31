# P4.1 工作流接口与执行契约

P4.1 在 P3 `0.2.1` 控制、数据、执行和科学验收闭环上增加冻结 CWL 包和 Toil 工作流，当前发行版本为 `0.3.8`。R4.2 冻结最小 ExecutionPlan，R4.3 为完全静态的执行生成不可变 `flat_v1` 引擎包，R4.4 只改变 Execution 内部工作领取类别，R5 冻结内部 Toil profile，并在两种配置下验证运行和恢复逻辑；正式保持 chaining 关闭；公开请求接口保持兼容。本轮只支持本机受信节点；QGIS、制图和远程执行不在此接口范围。

## 冻结包

令 `P=/api/v1/projects/{project_id}`。`POST P/workflow-packages` 接收自包含 `WorkflowPackageCreate`，返回 `202 WorkflowPackageReceipt`。登记只持久接受来源；Platform Worker 负责纯编译，再由固定 CWL 镜像中的 `cwltool` 预检。只有查询状态为 `READY` 时才产生精确 `package_ref`，其内容包括 `package_id`、固定 `version_id=1` 和 `manifest_digest`。

同一幂等键及相同内容返回原包；同键不同内容返回冲突。包发布后不能覆盖。`GET P/workflow-packages` 分页列出包，`GET P/workflow-packages/{package_id}` 返回来源、编译结果、摘要或安全的拒绝类别。

`v7-cwl-p4-v1` 是 CWL 1.2 的受限子集：

- 支持顺序、并行、包内子工作流、布尔输入条件，以及 dotproduct、flat_crossproduct、nested_crossproduct scatter。
- 支持标量、record、array、nullable 和 File。根 File 必须绑定精确资产表示；节点 File 可来自同一工作流中唯一、已成功且摘要匹配的私有节点输出。
- 只允许包内文档和目录登记的两个算子；拒绝外部 import/include、用户 JavaScript、ExpressionTool、Shell、镜像、服务地址和宿主路径。
- 编译器生成的内部表达式只负责 scatter 序号和调用上下文，不能由请求者修改。单包最大 1 MiB、单文档最多 1,000 个步骤、子流程深度最多 8。

## 提交、预算与节点身份

`POST P/executions` 继续返回 P2/P3 `CommandReceipt`。工作流请求使用 `WorkflowTarget`、按工作流端口组织的 `inputs` 和 1–1,000 的 `max_node_executions`。旧 P3 单算子请求结构不变。

提交事务原子检查当前操作者、项目角色、任务要求修订、冻结包、所有叶子算子、精确根输入和任务额度。父 Action 预留 `max_node_executions` 次执行额度，但不占计算并发名额。节点实际获得许可时占一个并发名额；受信容器确认开始时消耗一次额度；条件跳过和确认未开始的节点不扣费。父执行收敛后释放未使用额度。未知活动继续保留占用。

节点 ID 是以下值的 SHA-256 派生标识：

```text
root Execution ID + 完整调用路径 + 每一层 scatter 序号
```

相同输入值的不同 scatter 实例有不同节点 ID。Toil 作业 ID 是可空诊断事实，不参与逻辑身份、请求摘要、幂等、预算或恢复判断。从 `/proc` 无法取得 Job ID 时 bridge 仍提交同一个节点；leader 恢复后 Job ID 变化只追加新的诊断关联。重复调度查询原节点：成功结果复用，在途结果继续跟踪，终态失败不换身份重算。旧 bridge 的 `engine_job_id` 内部字段继续接受并规范化，公开接口没有新增字段。

每个节点启动前重新检查当前成员权限、任务状态、要求修订、授权代次、工作流包、叶子算子、根输入以及派生输出许可。暂停返回等待并阻止新节点；恢复重新检查。许可后发生的撤销或取消按已进入执行窗口处理。

## 不可变 ExecutionPlan

新工作流在 Platform 准入时从冻结包、冻结根输入、实现锁和父 Action 身份确定性生成 `execution_plan`。计划直接保存在父 Action 请求和 Execution 的不可变 `executions.payload` 中，不新增计划表或运行状态副本。顶层固定包含 `schema_version`、包摘要、输入摘要、静态节点、有界延迟模板和计划摘要；每个静态节点保存完整调用路径、各层 scatter 序号、依赖、算子、实现摘要、输入绑定摘要、条件和输出端口。

可从冻结根输入确定的 scatter、条件和包内子流程必须在持久化前全部展开。静态可执行节点超过声明额度或 1,000 节点时，提交在引擎创建前拒绝。任何仍依赖运行期结果的 deferred template 均以 `DEFERRED_TEMPLATE_UNRESOLVED` 拒绝，不写入可执行记录。

bridge 提交的节点必须精确匹配静态节点，或匹配合法延迟模板；Execution 和 Platform 各自校验父执行派生的节点身份、调用路径、序号深度、算子和实现摘要。重启和 Toil leader 换代只读取最初保存的计划，不重新编译。`0.3.4` 及更早版本创建、没有计划的执行继续按原冻结 `compiled` 结果恢复；新执行不能省略由 Platform 生成的计划。

计划同时冻结 `EnginePackage`：源包摘要、计划摘要、策略、入口、文件、节点到 step 的映射和包摘要一起保存。新请求只生成 `flat_v1`：包内只有已展开的直接计算 step 和一个受信 gather，不含 scatter、tagger 或包装子流程。执行查询的 runtime 保留只读 `engine_strategy` 和 `engine_package_digest`。缺少冻结引擎包的历史终态记录只能审计查询，不能恢复或重试。

## 查询、成果和取消

`GET P/executions/{execution_id}` 返回父工作流状态，并在 `runtime` 中区分引擎、发布和清理事实。`GET P/executions/{execution_id}/steps` 分页返回 `node_id`、完整调用路径、scatter 序号、执行/尝试身份、状态、是否执行和 Toil 作业关联。

最终输出按父 Execution ID 作为一组原子发布。每个资产保存工作流、节点、输入、参数和实现来源；中间文件只保留在 Execution 恢复目录，不出现在资产列表。多输出缺失、摘要不符、来源不唯一或越界路径会使发布失败，不公开半组成果。

候选交付可以通过 `workflow_outputs` 按精确输出名和数组/record 路径选择已发布资产。现有完整率和趋势成果仍使用 P3 Decimal 检查器验收；其他工作流成功只证明执行和发布成功，不会自动完成科学任务。

现有取消接口持久请求取消父工作流。Execution 随后停止引擎、节点并与 Data 发布仲裁；已提交发布不会被删除。清理未知时接口保留 UNKNOWN 事实，不声称外部副作用已经消失。

## 恢复边界

Execution 在启动前保存稳定 JobStore、容器名、引擎代次和节点身份。Worker 重启先定位原容器；Toil 中断且原 JobStore 完整时以新引擎代次执行 `--restart`。恢复只有在所有平台节点成功、冻结输出完整且来源摘要重新核对后才能发布。缺少 JobStore、失败节点、冲突事实或不完整结果均不能用进程退出码掩盖。

Toil 容器固定使用 Docker init。容器停止时，监督程序把第一次 `SIGTERM` 或 `SIGINT` 转发给独立的 Toil leader 进程组，等待 20 秒后才发送 `SIGKILL`，并最多再等待 5 秒；Docker 在 30 秒执行最终兜底。只有子进程真实退出后才原子写入带 epoch 的退出事实。旧格式退出文件继续兼容；错误 epoch、非法字段和不完整 JSON 不作为可信退出事实。

Toil 容器没有数据库凭据或 Docker socket。它只持有该父执行的短作用域桥接凭据；受信节点仍由 Execution 创建 P3 受限算子容器。

## 冻结引擎 profile

Execution 首次接受父工作流时冻结内部 Toil profile。客户端、工作流包和节点请求不能选择或覆盖它；幂等重放和新 epoch 始终使用首次保存的 profile。容器标签、每个 epoch 的只读配置和执行 runtime 共同记录 profile ID 与摘要。执行查询只读返回 `runtime.engine_profile` 的 ID、摘要和 `chaining_enabled`，不返回内部完整配置。

R5 对照的两组 profile 仅在 `disable_chaining` 上不同。Toil 版本、重试、核心数、作业数和默认内存均由标准库白名单精确校验，不能转化为任意命令参数。`0.3.7` 创建且没有 profile 的执行按 chaining-off 兼容恢复。R5 的真实 Toil 安全资格测试已经通过。性能 A/B 受到同机仓库外负载干扰，暂列为待复测，不用作本轮逻辑验收门槛；在没有可靠证据证明收益前，`0.3.8` 正式 profile 保守固定为 `toil-single-host-v2-chaining-off`。
