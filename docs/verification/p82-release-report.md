# P8.2 升级、回滚与 N-1 兼容报告

> HISTORICAL / SUPERSEDED. 本文记录 P8.2 / 0.15.1 初始验收；当前活动 Release 为 0.15.3，见机器可读 pointer。

**结论：P8.2 `0.15.1` 通过。** 单机维护窗口升级、安全停车、schema v2 备份、提交前回滚、N-1 冻结执行恢复和服务间发布摘要校验已实现。真实活动 QGIS/Native/StaticMap 工作流从 `0.15.0` 的 epoch 1、原 JobStore 恢复为 `0.15.1` 的 epoch 2，没有更换父 Action、节点、Execution、预算或成果身份。

结构化证据见 [p82-release-evidence.json](p82-release-evidence.json)，运维流程见 [P8.2 维护窗口升级与回滚](../operations/p8-upgrade-rollback.md)。

> 2026-10-17 运行态整改：本报告以下摘要保留为原始 P8.2 验收基线，不再充当在线运行指针。当前激活发布已由 [P8.2 / 0.15.3 修复与发布报告](p82-0153-remediation-report.md) 取代，机器可读指针见 [p82-current-release.json](p82-current-release.json)。

## 实际交付

- Release Lock schema v3：N-1 发布、兼容目录/镜像、API/备份/schema 和最低运行要求全部锁定。
- 不可覆盖发布目录和 canonical env 原子提交。
- 四服务内部 `X-V7-Release-Digest` 校验；旧 CWL bridge 仅在受信 workflow 路由下兼容。
- 部署级容器标签：deployment、Release、Action、Execution、attempt/epoch、实现和 EnginePackage。
- `park-manifest.json`：活动执行、远程身份、JobStore 关键摘要和等待事实。
- schema v2 冷备份：允许已证明停车的可恢复事实，继续验证和恢复 schema v1 备份。
- `scripts/p8-upgrade.sh`：plan、prepare、apply、verify、commit、rollback。

原始验收基线 Release digest 为 `sha256:360216cb3c5bab6e71d760e2445f056d17e85599aa82482427978820a07d73cb`，应用镜像为 `sha256:5ab06775879a89a25968b902efdda92d8c905dfa7778ade394c76f177393dbdc`。受控源码摘要为 `sha256:99578cfd7dbd5a909d49a9eb53fa09b338d2a648f93c0ee1fbe87a38f3c3cd7f`。11 个 Python wheel 和 Capability Studio 均为 `0.15.1`；业务数据库版本保持不变。不可覆盖发布目录为 `.p8-runtime/releases/0.15.1-360216cb3c5bab6e71d760e2445f056d17e85599aa82482427978820a07d73cb`。

## 真实活动恢复

最终候选通过操作 `p82-canonical-upgrade2`，从独立 Compose project、端口、数据库和状态目录中的 `0.15.0` 停车状态升级。停车事实为：

- 父 Action `e1b4d51a8b284346aebfbb856eac5b40`；
- 父 Execution `e89c436c265e2217a7e83504280ac3307`；
- EnginePackage `sha256:7770a79b944a389d75ce052b3c61a930afab75dc946d87c6d41b140df0e4fef3`；
- epoch 1，JobStore `shared/rootJobStoreID` 摘要 `sha256:6065636d41b247ebfae55d74d47c30dd23c0d0b0e4dd940819ef16bfcaa8970e`；
- Buffer 节点已登记但未执行。

提交并解除暂停后，原 JobStore 在 epoch 2 恢复，Buffer、Summary、StaticMap 三个原节点各有且仅有一个 Execution，全部成功。Data 只有一个父 publication，状态 `COMMITTED`；预算为 `used=3、reserved=0、in_flight=0`。没有运行中的测试科学容器。

`0.15.0` 客户端脚本在 `0.15.1` 上重新读取原 Task、Acceptance、EvidenceRef、预算和空间工作流身份，接口保持兼容。

## 测试结果

```text
bash scripts/check_p8.sh --phase upgrade
19 passed

bash scripts/check_p7.sh --phase compatibility
922 passed in 2491.49s (0:41:31)

Capability Studio
前端 7 passed；A1–A7 后端 117 passed

uv run --locked python scripts/verify_distribution.py
11 wheel/sdist、仓库外安装、四服务迁移、P3、Toil、QGIS、制图全部 PASS

ruff check / format --check（apps、packages、scripts、tests）
PASS；531 files formatted
```

升级故障演练在 `BARRIER_ACTIVE`、`BACKED_UP`、`TARGET_INSTALLED` 和 `TARGET_VERIFIED` 四个提交前阶段分别进入 `ROLLED_BACK`，源服务和持久身份恢复。状态机测试还验证 Release/Catalog/镜像错配、park manifest 篡改、canonical env 原子提交、提交后禁止自动回滚和旧备份兼容。

最终候选重放前曾发现同一 Docker daemon 上遗留的另一套验收实例保留了相同 Execution ID 的已退出科学容器。新 deployment 按标签不一致规则保守拒绝接管，没有重算或扣账。清理已退出的旧验收容器、从原停车备份重新恢复后，最终操作成功。这说明恢复演练必须独占对应稳定容器名；不能让两个 deployment 同时接管同一份执行身份。

## 边界

P8.2 只验收升级与恢复正确性，不设置负载性能门槛。固定负载、资源上限和组合故障属于 P8.3；安全扫描、离线签名、RC 交付包和真实远端阻断门槛属于 P8.4。本轮未进入这些阶段。
