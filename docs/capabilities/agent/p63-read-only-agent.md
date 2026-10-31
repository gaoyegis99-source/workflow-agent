# P6.3 上下文与只读 Agent

版本：`0.8.3`  
Platform schema：`0013_agent_read_tools`

P6.3 只允许 Agent 读取当前任务已有事实。模型可以形成计划、解释、提出澄清、等待或说明无法继续，但不能提交计算、登记研究修订、创建交付、发起验收、修改授权或增加预算。

## 上下文边界

`ContextCompiler v1` 每个决策帧生成 ContextManifest v2。不可覆盖政策、proposal schema 和工具目录位于固定层；Task 控制状态和预算来自 Platform 权威记录。Task 自由文本、用户回答、Data/Knowledge 内容和服务载荷均使用 `UNTRUSTED_DATA` 信封，即使其中出现“忽略规则”或伪造工具定义，也只作为数据交给模型。

编译器保留 10% 输出空间。必要 Task 要求、政策或工具目录放不下时返回 `CONTEXT_OVERFLOW`，不会截断后继续。可选观察只按完整对象纳入；未纳入对象在 manifest 中保存摘要、估算 token 和 `SECTION_TOKEN_LIMIT` 原因。

## 只读目录

目录固定 15 项：Task、要求、计划、Data 资产及语义、Knowledge 搜索及精确证据、算子/工作流/checker 发现、Execution 及步骤、Research 和 Acceptance 查询。每项冻结版本、参数字段、操作类别、作用域、幂等规则、观察类型和错误类别。模型输出不能增加目录项。

搜索命中只是 `SearchHit`。`knowledge.read_evidence` 才生成精确 EvidenceRef 和读取回执。Agent 的稳定 ToolRequest ID 同时作为 Knowledge 读取回执 ID，因此 Worker 崩溃重放不会制造另一份读取身份。

## 权限和恢复

Agent Worker 不保存用户 Bearer Token。每次读取以 AgentRun 冻结的创建者身份重新检查当前项目成员关系，并以服务身份调用内部端口。Data 和 Knowledge 结果还按当前 Task 的有效精确资源 grant 过滤；Execution 必须属于同一 Task。项目成员被移除、Task 修订、暂停或取消后，不再执行远程读取。

ToolRequest 在网络调用前落库。成功、拒绝、找不到和暂时不可用分别形成机器可读观察。服务载荷先持久化，再唤醒下一决策帧。模型 call ID 仅负责模型调用；ToolRequest 身份由 AgentRun、frame、proposal 位置和规范请求摘要生成。

## 部署

升级顺序：

```text
停止 Platform API 与 Agent Worker
→ 安装十个 0.8.3 发行包
→ 显式升级 Platform 到 0013_agent_read_tools
→ 生成并封存 agent-tool-catalog.json
→ 核对 Platform 与 checkpoint schema
→ 启动 API 和 agent-worker
→ 运行 readonly 与 all 门禁
```

目录管理：

```bash
agent-tool-catalog generate --output /etc/groundwater-v7/agent-tool-catalog.json
agent-tool-catalog check --output /etc/groundwater-v7/agent-tool-catalog.json
```

生产 Worker 额外要求 `V7_AGENT_TOOL_CATALOG`、三个服务 URL/凭据、模型目录/凭据、Platform 与 checkpoint 数据库。启动只检查 schema，不自动迁移。

回退到 `0.8.2` 前必须确保没有 P6.3 ToolRequest；否则旧进程无法解释新的读取账本，应恢复升级前数据库备份。Knowledge 的稳定读取回执兼容普通随机回执，不需要 Knowledge schema 迁移。

