# V7 清单执行范围决定

日期：2026-10-19  
状态：**RECORDED FOR CHECKLIST EXECUTION**  
依据：用户要求完整执行 [V7 完成度清单](../v7-completion-checklist.md)。

## 决定

清单中每个未完成项默认按实施处理。只有现有已接受的[条件能力范围 ADR](v7-conditional-capabilities-scope.md)明确延期的子能力继续延期；本决定不新增排除项，也不把未实现能力写成已完成。逐项决策、负责人、状态和依据见[机器可读决策矩阵](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/checklist-implementation-decisions.json)。

## 沿用的 ADR 边界

| 子能力 | 当前决定 | 仍在实施范围的工作 |
| --- | --- | --- |
| Join 和多输入 Join | 延期至出现具名真实用例并满足 ADR 复评条件 | 现有单输入能力及清单的其他表格工作 |
| 自动跨主机 HA | 延期；当前选择单机支持目标 | 冷备份、恢复、RPO/RTO、全新宿主恢复和单机故障域验证 |
| OIDC/SSO | 延期；继续使用管理员签发、可撤销的 Bearer Token | Token 生命周期、权限边界和 UI/文档如实说明 |
| 手机与小于 1100px 窄屏 | 延期；保留桌面支持边界 | 桌面支持矩阵、可访问性和正式说明 |
| 公共 Marketplace、跨项目共享和自助安装 | 延期 | 固定 Catalog、管理员审核和项目内工作流 |
| 用户自助 Agent authoring、动态工具和用户代码 | 延期 | 受控 Agent Task、固定 Catalog/Runtime 和多 Agent 支持 |

其他清单中明确列出的高级表格算子、多 peer 远程执行、远程 QGIS/Toil leader、制图导出、扩展 CWL、实时协作、跨平台导出与多 Agent 均按完整清单目标实施；它们仍须通过各自安全、科学和发行门禁。跨主机自动接管的延期不排除多 peer 远程执行。

## 执行限制

这份记录关闭的是“为未完成项作出实现/排除决定”的治理要求，不表示这些功能已实现或通过验收。状态仍以清单和登记表为准；每项完成前必须补齐对应证据。
