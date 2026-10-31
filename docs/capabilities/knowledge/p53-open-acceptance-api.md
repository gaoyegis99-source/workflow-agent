# P5.3 开放科学验收接口

P5.3 保留 `p3-timeseries-v1`，并为 `TaskSpec.acceptance_spec` 增加判别成员
`open-science-v1`。开放验收明确区分 `EXPLORATORY` 和 `VERIFIED`。前者允许带着完整列出的限制完成；后者要求所有必要项获得有效覆盖。

`open-science-v1` 包含 `requirements`、`required_outputs`、`allow_manual_review` 和
`max_check_executions`。每条 requirement 指定一到多种固定检查类型：运行完整性、数据有效性、方法前提、结果复算、主张支持或人工评审。客户端不能上传规则表达式或检查代码。

原有 `POST /api/v1/projects/{project_id}/tasks/{task_id}/acceptance` 编译并冻结
CoveragePlan。`GET .../acceptance/{acceptance_id}` 返回计划、逐项结果、评审记录及完成有效性。新增：

```text
POST /api/v1/projects/{project_id}/tasks/{task_id}/acceptance/{acceptance_id}/reviews
```

该接口仅允许 OWNER 对计划中状态为 `REVIEW_REQUIRED` 的 `HUMAN_REVIEW` 项提交决定。请求固定计划修订、检查项、资格声明、准则、范围、决定和理由。人工决定不能覆盖摘要错误、来源错误、缺失成果、确定性失败或已撤回证据。

检查项结果为 `PASS`、`FAIL`、`INDETERMINATE`、`REVIEW_REQUIRED`、
`NOT_APPLICABLE_WITH_REASON`、`ERROR` 或 `STALE`。通用检查器无法独立复算一种结果时返回 `INDETERMINATE`；它不会把缺少检查能力解释为通过。

当前固定通用检查器只对运行完整性和已登记的结构化方法前提作确定判断。仅有技术 manifest 不能使 `DATA_VALIDITY` 通过；仅有用户标记为 `SUPPORTED` 的主张和来源边，也不能使 `CLAIM_SUPPORT` 通过。没有登记的领域检查器时，这两类检查与 `RESULT_REPRODUCTION` 返回 `INDETERMINATE`。每份检查报告必须完整且唯一地覆盖冻结计划中的所有自动检查项，检查类型、计划摘要和汇总状态不一致时整份报告拒绝入账。

Knowledge 的生命周期事件经内部租约接口投递。证据撤回后，历史验收报告保持不可变，Task 仍为 `COMPLETED`，当前 completion 增加 `validity=STALE` 和原因；任务查询也返回当前有效性。当前版本不重新打开已完成任务，因此重新验证需建立新任务和新交付。原任务内的专用重验流程仍待后续实现。
