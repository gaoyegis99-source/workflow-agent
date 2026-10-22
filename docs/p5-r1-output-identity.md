# P5-R1 精确成果身份

`open-science-v1.required_outputs` 在新 TaskSpec 中使用成果角色。角色说明需要的结果类型、算子及版本、输出端口和 schema；它不引用尚未产生的 Execution。

成果产生后，`POST /api/v1/projects/{project_id}/tasks/{task_id}/deliverables` 使用 `role_bindings`。直接算子输出冻结 Action、Execution、端口、AssetRef、表示和摘要。工作流输出额外冻结父 Action/Execution、节点 Action/Execution、完整调用路径、scatter 位置、父输出端口以及数组或 record 路径。

Platform 不直接接受这些派生身份。服务重新读取 Platform Action 和节点账本、Execution 发布回执及 Data manifest，并将请求与规范化引用逐字段比较。Action、Execution、路径、端口、资产、表示、摘要或 schema 任一不一致都返回 `409 INVALID_BINDING`。相同资产绑定到不同角色会产生不同 Deliverable 摘要。

新 VERIFIED TaskSpec 必须使用角色契约。提交验收时还必须提供 `analysis_registration_id`；该登记必须属于同一项目和任务、对应当前 TaskSpec revision，且未被后续分析修订替代。CoveragePlan schema v2 冻结该登记和角色映射。

旧 `output_id`、`executions` 和 `workflow_outputs` 继续用于历史读取、P3 验收及临时 EXPLORATORY 兼容。旧身份的探索报告包含 `LEGACY_OUTPUT_IDENTITY`。旧 VERIFIED 记录可以读取，但不能发起新的验收；应创建使用角色契约的新 TaskSpec revision。

新发布资产的表示 manifest 写入 `schema_id`。历史 P3/P4 资产保持原字节和 manifest，通过受信的算子、版本及端口兼容目录推导 schema，并在 BoundOutputRef 中记录 `schema_source=COMPATIBILITY_CATALOG`。

P5-R1 仍使用 P5.3 的单一通用 checker。领域复算和多 checker 组合属于 P5-R2。
