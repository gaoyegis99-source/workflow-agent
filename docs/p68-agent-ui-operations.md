# P6.8 / 0.8.8 Agent 页面与运维

任务页新增 Agent 状态、计划修订、开放问题、结构化回答、决策/修复时间线、等待依赖、runtime 恢复和模型用量。令牌仍只保存在页面内存。事件流使用带 Bearer Token 的 `fetch`，以 `wake_revision` 为游标；断线重连查询 Platform 当前投影，不把浏览器当作执行进程。

时间序列任务可选择 `DIRECT` 或 `AGENT`。AGENT 创建请求显式提交执行额度、在途上限、模型调用数和决策帧上限；模型 profile、工具、实现和命令仍由部署冻结，页面不能选择。选中 AGENT 任务时，人工“运行完整率／趋势／工作流”按钮禁用，避免页面绕过 Agent 行动账本。OWNER 仍需授予精确输入和目标范围。

真实 Chromium 门禁从页面创建 AGENT 任务，随后由 Agent Worker 调用真实 Data/Execution 完成两个算子、发布、独立验收和报告。刷新后 Token 输入为空，状态从 Platform 账本恢复。

`agent-admin` 提供 `list-waiting`、`list-blocked`、`reconcile` 和 `export`。导出只包含可见业务账本，不包含模型密钥、用户令牌或 framework checkpoint。CLI 不提供强制 PASS、伪造结果或任意数据库更新。

当前 SSE 每次返回一个投影后由客户端重连，优先保证鉴权和恢复语义；它不是高吞吐消息总线。
