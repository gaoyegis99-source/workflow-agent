# P7.0 / 0.10.0 能力边界阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

## 完成内容

- 建立部署冻结的 Operator Catalog、确定性摘要、编译与双服务校验。
- 将五个既有计算算子迁入统一目录，未增加新算子。
- 分离 compute 与 checker 执行注册表；系统工具保持独立。
- Action、工作流叶子节点、容器配置和成果 provenance 冻结精确算子身份。
- 增加 ACTIVE、DISABLED、REVOKED 的准入、恢复及发布语义。
- 新 Agent Tool Catalog 使用显式版本的 `execution.submit_operator@2.0.0`；旧 P6 目录可恢复。

## 版本和迁移

发行版本为 `0.10.0`。Platform 保持 `0018_agent_acceptance_identity`，Execution 保持 `0006_remote_reliability`，没有数据库迁移。

## 范围

本轮没有加入 `table-profile` 或 `table-validate`，没有用户镜像、代码、命令或动态 Python 包入口。独立算子试点属于 P7.1a。

## 问题处理

- 将 Acceptance checker 与工作流父协调 Action 从普通算子许可中分离，叶子计算继续逐项校验。
- 修复后续 DTO 默认字段改变早期任务、修订和授权幂等摘要的问题，历史请求按原始字段重放。
- 将工作流目录恢复校验限定为不可变算子语义与实现 ID；正式部署的完整目录和镜像摘要由双服务 readiness 与容器创建共同核验。
- 发布前先恢复 Data 已有提交事实，再应用 `REVOKED` 仲裁，避免响应丢失后出现“成果已公开但执行被误报拒绝”。
- 父工作流先核验全部私有子成果和取消事实，再检查生命周期并提交，残留私有文件不能绕过原子发布。

## 验证

- `bash scripts/check_p7.sh --phase catalog`：12 项通过。
- P1 选择规则：13 项通过。
- contracts、integration、architecture 与 P2：235 项通过。
- P3：87 项通过。
- P4 全阶段：228 项通过。
- P5：86 项通过。
- P6：1512 项通过。
- Ruff 检查、格式检查和 compileall 通过。
- 10 个 `0.10.0` wheel 构建完成，并在仓库外的新虚拟环境安装、导入和校验目录。

共覆盖 820 项互不重复的测试，无失败或跳过。结构化证据见 `p70-operator-catalog-evidence.json`。

本轮同时封存 `p70-operator-catalog.lock.json`、`p70-checker-catalog.lock.json` 和 `p70-release-lock.json`。release lock 关联 10 个 wheel、六个已验证运行镜像、两个能力目录和关键源码摘要。
