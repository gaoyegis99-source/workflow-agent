# P6.7 / 0.8.7 有限自主修复

`agent_delivery_attempts` 保存每个工具分支的请求摘要、顺序、重试来源、结果类别和停止原因。成功、失败、未知和停止均保留，模型不能删除失败历史。

同一精确分支出现两次相同确定性失败后进入 `BLOCKED / REPEATED_FAILURE_LIMIT`。外部状态未知时禁止创建新 ToolRequest，必须由原 Action、Execution 或 Acceptance 身份继续对账。Acceptance ERROR 可通过 `retry_of` 明确重试；既有 PASS 不重复运行。Acceptance FAIL 仍不能靠换幂等键变成 PASS。

目录不暴露 checker 选择、镜像、容差、隐藏答案或完成写入。模型只能提出受控请求，科学事实继续由 Data、Execution、Knowledge 和 Acceptance 产生。
