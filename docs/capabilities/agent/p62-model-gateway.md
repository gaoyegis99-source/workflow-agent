# P6.2 模型网关、预算与恢复

版本：`0.8.2`
Platform schema：`0012_agent_model_calls`
Checkpoint schema：`0001_langgraph_checkpoint`

P6.2 在 P6.1 ledger 与 LangGraph 边界上增加真实模型调用协议。Platform 仍是 Task、AgentRun、DecisionFrame、模型预算和调用事实的权威所有者；Agent Worker 执行供应商 HTTP I/O。模型不能直接访问数据库、Data、Knowledge、Execution 或用户工具。

## 冻结配置

部署必须同时生成运行时 profile 和模型目录：

```bash
uv run python scripts/build/generate_agent_runtime_profile.py \
  --output /etc/groundwater-v7/agent-runtime-profile.json

uv run python scripts/build/generate_model_catalog.py \
  --profiles /etc/groundwater-v7/model-profiles.json \
  --default-profile approved-model-v1 \
  --output /etc/groundwater-v7/model-catalog.json
```

模型 profile 固定 provider、模型快照、endpoint、structured-output 能力、上下文与输出上限、价格修订和凭据环境变量名。目录不保存凭据值。生产 API 设置：

```text
V7_AGENT_RUNTIME_PROFILE=/etc/groundwater-v7/agent-runtime-profile.json
V7_MODEL_CATALOG=/etc/groundwater-v7/model-catalog.json
```

Agent Worker 使用同一文件，并在进程环境中设置 profile 的 `credential_env`。API 只配置其中一个文件时 `/api/v1/ready` 返回 503。客户端不能在 Task 或模型请求中覆盖 profile。

## 调用与预算

AGENT Task 必须显式提交模型预算，包括调用、输入 token、输出 token、reasoning token、估算费用、Agent 墙钟时间和决策 frame 上限。每次供应商 I/O 之前，Platform 在一个事务内冻结请求并创建 ModelCall、预算预留和恢复记录。

同一个 ModelCall ID 是供应商幂等键。429、5xx、超时、断流、响应丢失和普通 404 不会释放预留，也不会换 ID。明确 `REJECTED/CANCELLED + executed=false` 才释放未执行预留。已执行但 usage 缺失时，调用次数计入消耗，token 和费用继续保守预留并显示为 unknown。

P6.2 的上下文仅包含当前 TaskSpec、已接受计划和测试工具目录。响应必须符合严格的 DecisionProposal 联合。格式错误允许一次新的修复调用；该调用单独预留和计费。第二次格式错误使 AgentRun 进入 `BLOCKED / MODEL_OUTPUT_INVALID`。

只读用量接口：

```text
GET /api/v1/projects/{project_id}/tasks/{task_id}/model-usage
```

## 升级与验收

升级顺序：

```text
停止 Platform API 和 Agent Worker
→ 安装十个 0.8.2 发行包
→ 显式升级 Platform 到 0012_agent_model_calls
→ 生成并核验 runtime profile 与 model catalogue
→ 启动 API 和 Agent Worker
→ 检查 /api/v1/ready
→ 运行 P6.2 门禁
```

```bash
bash scripts/checks/check_p6.sh --phase model
bash scripts/checks/check_p6.sh --phase all
```

`model` 覆盖 P6.2、P6.1、P6.0、P2、契约和架构边界；`all` 在已有聚焦证据上补 HTTP/OpenAPI 和仓库外发行安装。本阶段不重复 P4.3.2 的长远程故障矩阵，继续使用 P6.1 已通过的 698 项完整基线。

P6.2 不开放 Data/Knowledge 读取和业务工具执行。迁移回退到 `0011` 会删除模型调用和费用事实，只允许在没有 P6.2 Agent Task 和 ModelCall 时执行；已有记录时应恢复升级前数据库备份。
