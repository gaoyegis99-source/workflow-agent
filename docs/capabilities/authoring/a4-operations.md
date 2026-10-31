# Capability Studio A4 / 0.14.3 部署、维护与回退

## 升级

1. 等待在途工作流 Trial、Acceptance 和 Agent Action 收敛。
2. 停止 Platform API、Platform Worker、Authoring Worker、Agent Worker 和 Execution Worker。
3. 安装完整 `0.14.3` wheel 集和 CWL runtime，部署新的 Studio 静态资源。
4. 显式执行 `platform-admin migrate`，确认 Platform schema 为 `0022_operator_proposals`；Data、Execution、Knowledge schema 不变。
5. 生成并核验 Operator、Checker、Tool Catalog、artifact 清单和显式镜像摘要。
6. 启动服务，运行 `bash scripts/checks/check_authoring.sh --phase operator-registration`、P7 catalog/compatibility 和发行安装检查。

启动仍不自动迁移。直接 Compose 部署必须设置并固定 `V7_OPERATOR_CATALOG`、`V7_CHECKER_CATALOG`、`V7_AGENT_TOOL_CATALOG`、`V7_DEPLOYMENT_ID`、`V7_DEPLOYMENT_REVISION` 和七个镜像摘要。

## 维护者审核

审核决定绑定精确 proposal revision、材料摘要和证据摘要：

```bash
platform-admin operator-proposal-review \
  --project-id PROJECT \
  --proposal-id PROPOSAL \
  --revision 1 \
  --decision APPROVED \
  --maintainer-id release-maintainer \
  --evidence-digest sha256:... \
  --reason "Exact source, tests and contracts reviewed"
```

材料改变后必须创建新 revision 并重新提交。CLI 拒绝对非当前 revision、非 `SUBMITTED` 状态或非法证据身份写入决定。

## 导入部署证据

实际受控发行完成后执行：

```bash
platform-admin operator-proposal-deployment-import \
  --project-id PROJECT \
  --proposal-id PROPOSAL \
  --revision 1 \
  --maintainer-id release-maintainer \
  --evidence-digest sha256:...
```

命令从当前固定 Catalog 和显式部署证据读取实际算子、实现、镜像及部署修订，不接受调用者覆盖这些字段。语义、实现、镜像、Catalog 文件或部署证据任一不一致都会拒绝导入。项目 OWNER 没有对应 HTTP 路由。

## 回退

`0022` 保存不可变审核和部署历史。若已有 A4 数据，回退到 `0.14.2` 必须恢复升级前数据库备份，不能通过删除申请、事件或回执来强制降级。Catalog 生命周期变化不需要重写历史；API 会按当前部署重新投影匹配状态。
