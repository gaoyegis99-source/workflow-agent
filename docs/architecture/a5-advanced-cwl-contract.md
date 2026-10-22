# Capability Studio A5 / 0.14.4 受限高级 CWL 契约

## 支持范围

A5 在既有 `v7-cwl-p4-v1` 包内支持以下可编辑语义：

- 包内 Workflow 子流程，最大调用深度 8；
- 精确布尔输入条件 `$(inputs.<identifier>)`；
- `dotproduct`、`flat_crossproduct`、`nested_crossproduct`；
- 标量、可空值、数组、record、File；
- 受限 `linkMerge` 与 `pickValue`；
- 自包含 JSON/YAML 导入及源码、画布的无损往返。

所有叶子节点必须引用 Operator Catalog 中的精确算子版本和本地受信实现。客户端不能提供命令、镜像、脚本、表达式、服务地址或宿主路径。`$import`、`$include`、`baseCommand`、`ExpressionTool`、Shell、任意 JavaScript、Docker requirement 和外部 URL 明确拒绝。

## 安全导入

`POST /api/v1/projects/{project_id}/authoring/imports` 接受最多 1 MiB 的 JSON 或 YAML 文本。解析器拒绝重复键、YAML alias/anchor、自定义 tag、非 JSON 标量、非有限数值和超出 JavaScript 精确整数范围的数值。导入包必须有本地入口，所有 `run` 必须解析到包内 Workflow 或受信实现。

导入只返回规范 source 与摘要，不创建 Revision。用户必须显式保存新 Revision；并发和幂等仍使用既有 Revision 规则。

## 编译、执行和身份

正式 Validation、Candidate 和 Trial 使用同一个受限编译器身份 `a5-v1`。Candidate 事务外确定性编译，最终事务重新核对 Revision、Validation、Catalog 和环境。编译失败不会创建候选执行或占用预算。

静态 scatter 在 ExecutionPlan 中展开，节点身份由父执行、完整调用路径和各层 scatter 序号决定。重复输入值不会合并节点。静态展开最多 1,000 个计算节点；动态展开继续受父额度和运行时节点上限限制。恢复复用原 Package、ExecutionPlan、EnginePackage、Action 和节点身份。

## Trial 覆盖事实

新 Trial 冻结 `workflow-trial-case-v2`：

- `planned_steps` 记录预期执行、条件跳过或动态状态；
- `deferred_templates` 记录动态节点模板；
- `required_outputs` 与断言冻结输出类型、路径和 Schema；
- `execution_plan_digest` 绑定计划身份。

判定结果记录各调用路径的计划、观测、启动、成功和跳过数量，列出未覆盖条件/动态分支，并保存最终数组/record/null 形状。实际节点不属于静态计划或冻结模板时，Trial 失败为 `COVERAGE_IDENTITY_FAILED`。

全条件跳过和空 scatter 可以针对该冻结输入形成样例 PASS，但 Release 至少需要一条 PASS Trial 实际启动计算。样例 PASS 只证明样例和断言，不等同于科学 VERIFIED；科学完成仍由 Deliverable、CoveragePlan 和独立 Checker 给出。

## 兼容边界

旧 A3 `workflow-trial-case-v1` 按其完整静态 required steps 投影为覆盖事实，不会在升级后被误判为未知动态节点。旧 WorkflowPackage、Release、成果和验收不重写。A5 没有新增数据库表或迁移。

不属于 A5：外部源码抓取、用户代码、任意表达式、可视化脚本编辑、工作流市场、Agent 创建工作流、跨平台导出和 Join 自动生成。

