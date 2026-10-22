# P6.5 / 0.8.5 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

实现冻结等待依赖、来源水位、丢失事件对账和运行时 epoch 恢复记录。中间业务状态继续入账，但不会反复唤醒模型。

统一门禁：`bash scripts/check_p6.sh --phase recovery` 与 `bash scripts/check_p6.sh --phase all`。
