# P6.2 / 0.8.2 模型网关与预算报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

状态：**PASS**  
完成时间：2026-10-15T02:12:58.732181+00:00

## 完成内容

- Platform `0012_agent_model_calls` 保存模型额度、逐调用预留、冻结请求、供应商观察和恢复对账材料；模型 I/O 前已在同一事务完成预算准入。
- 部署使用摘要冻结的 runtime profile 和 model catalogue。客户端不能指定供应商、模型、endpoint、实现、价格或凭据；凭据只从 Agent Worker 环境读取。
- Agent Worker 支持真实 Responses HTTP 适配、确定性 fake 和 record/replay。每个 ModelCall ID 同时作为稳定供应商幂等键，响应丢失、429、5xx、超时和临时 404 均查询或重提原身份。
- structured output 经过严格判别联合校验。首次格式错误最多触发一个独立、计费且有预算预留的 JSON 修复调用；再次错误使 AgentRun 明确阻塞。
- API 提供只读 model usage，分别显示使用、预留、结果未知和 usage 未知；墙钟、决策轮数、调用、token、reasoning 和估算费用均有上限。

## 测试结果

- P6.2 专项：25 passed，0 failed，0 skipped。
- P6.2、P6.1、P6.0、P2、契约和架构聚焦回归：291 passed，0 failed，0 skipped，用时 90.16 秒。
- 当前变更的 HTTP/OpenAPI 回归：11 passed，0 failed，0 skipped，用时 2.118 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、运行时 probe、checkpoint 管理 CLI 和发行依赖检查通过。

## 已关闭问题

- 429 后的同一供应商请求保留 ModelCall ID，同时让本地观察 revision 单调递增，后续成功不会与限流事实冲突。
- Worker 接管、供应商响应丢失和 provider ref 暂不可见时不会创建第二个调用、第二份预留或第二个 proposal。
- usage 缺失不会被当作零费用；调用次数已结算，token 和费用预留继续保留并在 API 中显示未知。
- Platform 与 Agent Worker 对运行时和模型目录做相同摘要核验；单边配置使 readiness 不通过。

## 范围与结论

P6.2 已打通“冻结上下文 → 预算预留 → 真实模型协议 → 严格 proposal → ledger 接受”的模型决策链。它尚不读取 Data/Knowledge，也不能提交业务工具或科学计算。根据本轮“不做过度兼容测试”的要求，没有重新跑完与本次变更无关的 P4.3.2 长故障矩阵；P6.1 的 698 项完整基线保留，本轮全量尝试停止前已有 532 项零失败。P6.2 通过并结束；下一轮应进入 P6.3 上下文编译和只读工具，本轮未提前实现。
