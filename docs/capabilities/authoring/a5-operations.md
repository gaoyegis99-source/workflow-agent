# Capability Studio A5 / 0.14.4 部署、升级与回退

## 升级

1. 等待活动 Validation、Trial、工作流引擎和发布操作收敛。
2. 停止 Platform API、Authoring Worker、Execution Worker 和 Agent Worker。
3. 安装完整 `0.14.4` 发行物及 CWL runtime。
4. 核对 Platform schema 仍为 `0022_operator_proposals`；Data、Execution、Knowledge schema 不变。
5. 核对 Operator、Checker、Tool Catalog，7 个镜像摘要、11 个 wheel 和 Studio 静态清单与 A5 Release Lock 一致。
6. 启动服务并运行 `bash scripts/checks/check_authoring.sh --phase advanced-cwl`。

A5 没有迁移，启动仍不自动迁移。编译器身份从 `a2-v1` 更新为 `a5-v1`，因此旧 PASSED Validation 保留历史可读，但不能冻结新的 A5 Candidate；应针对当前 Revision 重新请求 Validation。

## 运行

- Studio 同源入口仍为 `/studio/`；令牌只保存在内存。
- JSON/YAML 导入不会自动保存 Revision。
- Trial 使用已有 DIRECT Task、grant 和预算，不自动提权或增加额度。
- 条件跳过、动态 scatter、执行事实和成果发布分别展示；UNKNOWN 不显示为停止或失败。
- A5 不新增性能门槛，也不改变 Toil、计算 Worker 或预算配置。

## 回退

回退到 `0.14.3` 前先让 A5 Validation、Candidate、Trial 和工作流执行收敛，并停止写入。数据库无需降级，但 `0.14.3` 无法理解新 source 的高级编辑语义，也不能按 A5 覆盖规则判定新 Trial，因此不得混跑新旧 API/Worker。

历史 Revision、Package、Trial、Release、Action、预算、JobStore 和成果不得删除或重写。若 A5 Trial 仍在途，优先前向修复到 `0.14.4`；不要用旧 Worker 接管。
