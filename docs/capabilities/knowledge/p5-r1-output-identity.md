# P5-R1 精确成果身份

`open-science-v1.required_outputs` 在新 TaskSpec 中使用成果角色。角色说明需要的结果类型、算子及版本、输出端口和 schema；它不引用尚未产生的 Execution。

成果产生后，`POST /api/v1/projects/{project_id}/tasks/{task_id}/deliverables` 使用 `role_bindings`。直接算子输出冻结 Action、Execution、端口、AssetRef、表示和摘要。工作流输出额外冻结父 Action/Execution、节点 Action/Execution、完整调用路径、scatter 位置、父输出端口以及数组或 record 路径。

Platform 不直接接受这些派生身份。服务重新读取 Platform Action 和节点账本、Execution 发布回执及 Data manifest，并将请求与规范化引用逐字段比较。Action、Execution、路径、端口、资产、表示、摘要或 schema 任一不一致都返回 `409 INVALID_BINDING`。相同资产绑定到不同角色会产生不同 Deliverable 摘要。

新 VERIFIED TaskSpec 必须使用角色契约。提交验收时还必须提供 `analysis_registration_id`；该登记必须属于同一项目和任务、对应当前 TaskSpec revision，且未被后续分析修订替代。CoveragePlan schema v2 冻结该登记和角色映射。

活动接口不再接受 `output_id` 或没有 `role_bindings` 的 Deliverable。历史记录正文仍可作为审计数据读取，
但不得重新执行、重试或生成新的 CoveragePlan。维护迁移只接受已经具备唯一角色绑定的记录；无法确定
角色语义的记录会阻断整批切换。

新发布资产的每个可绑定表示必须在不可变 manifest 中写入 `schema_id`，BoundOutputRef 固定记录
`schema_source=MANIFEST`。系统不再通过算子名称、版本和端口推导缺失 schema；旧资产需要发布带明确
schema 的新版本后才能参与新的 Deliverable。

P5-R1 仍使用 P5.3 的单一通用 checker。领域复算和多 checker 组合属于 P5-R2。
