# ADR-V7-SCOPE-20261019：条件能力的当前范围

日期：2026-10-19  
状态：**ACCEPTED FOR CURRENT V7 SCOPE**  
适用基线：V7 `0.15.3` 当前发布及尚未激活的 `0.15.5` 候选；本 ADR 不授权部署候选。

## 背景

V7 完成清单要求 Join、跨主机 HA、SSO、移动端、Marketplace 和 Agent authoring 分别作出实现或 ADR 排除决定。现有产品方案把首期限定为可信操作者、多项目、单台 Linux 私有服务器；Capability Studio 计划明确排除 Join、多输入协议、公开市场、Agent 自主登记或发布；README 也声明桌面最小宽度。P8.2 已实现维护窗口停车、冷备份和升级恢复，但这不等于跨主机自动故障转移。

本 ADR 将上述边界正式纳入当前 V7 完成目标。`DEFERRED` 表示当前不承诺交付该能力，并不是永久禁止；只有列明的重评条件成立后才重新立项。没有声明排除的灾备恢复、RPO/RTO、备份保留和新宿主恢复仍属于 G4 必须验收范围。

## 决定

| 能力 | 当前决定与承诺 | 用户影响 | 当前替代路径 | 重新评估条件 |
| --- | --- | --- | --- | --- |
| 表格 Join | `DEFERRED`。当前 V7 不提供多输入 Join，也不把单输入 `TABULAR_V1` 解释成可读取第二张表。 | 用户不能在 V7 内直接按键合并两个精确资产；平台也不声称能追踪未登记的外部合并关系。 | 在数据治理允许时，可在 V7 外生成一个新派生表并作为新 Asset 导入；用户或管理员应随该 Asset 保存来源和转换说明。现有单输入算子与 Aggregate 继续可用。 | 出现具名真实业务用例，明确左右输入角色、键、基数、null 语义和输出上限；随后先完成 P7.3b 多输入身份/授权/恢复/来源基础，再单独立项受限 Join。依据：[Join 复评](../architecture/p73-join-aggregate-reassessment.md)。 |
| 跨主机 HA | `DEFERRED`。当前正式支持目标选择 G4 目标 A：单机部署，不声明跨主机自动 HA。 | 宿主或本机状态存储故障可能造成服务中断；不会自动切换到另一主机。 | 使用维护窗口、已验证的冷备份和人工恢复流程。该路径不保证零停机，也不豁免 G4 的 RPO/RTO、独立故障域备份和全新宿主恢复演练。 | 目标用户明确需要自动接管，且已经冻结数据库复制、共享/复制状态、执行节点 fencing、网络分区策略、预算唯一结算以及可实测 RPO/RTO。 |
| OIDC / SSO | `DEFERRED`。当前使用管理员签发、可撤销的 Bearer Token 与项目 OWNER/EDITOR/VIEWER 角色；不承诺企业身份提供方登录或自动用户同步。 | 用户需通过管理员流程取得和维护 Token；员工离职、MFA、组同步等仍由外部身份和管理员流程处理。 | 使用现有 principal/token CLI 签发与撤销流程，并按项目分配角色；Gateway 前部署 TLS，令牌按运维流程保管和轮换。 | 有明确组织要求接入 OIDC/SAML，并完成 subject 生命周期、项目角色映射、审计身份、撤销/停用时延、密钥轮换和故障降级设计；权限真相仍由 V7 项目成员账本持有。 |
| 移动端与窄屏 | `DEFERRED`。当前产品界面只承诺宽度至少 1100px 的桌面浏览器。 | 手机和窄屏平板不在支持范围，可能无法完成主要操作。 | 使用满足最小宽度的桌面浏览器；窄屏不得作为受支持部署入口宣传。 | 有明确移动端用户场景和目标设备矩阵，完成响应式布局、触控、键盘/屏幕阅读器和全关键流程视觉回归。 |
| 公共 Marketplace | `DEFERRED`。当前没有公开算子/工作流市场、跨项目共享、购买或自助安装承诺。 | 用户不能自行发布和发现跨项目可安装能力，也不能期待市场审核、许可或供应商分发服务。 | 使用当前管理员审核、固定 Catalog、部署 Release 和项目内工作流版本；接入材料不等于已批准或可执行。 | 明确市场参与者和租户边界，并具备包来源审查、许可证、SBOM/漏洞门禁、签名、发布/撤回、隔离、计费与升级回滚责任。 |
| 用户自助 Agent authoring | `DEFERRED`。用户不能在 UI 中创建任意 Agent、动态工具、任意代码或可自行发布的 Agent 工作流。当前 Agent 能力仅使用部署固定的 Runtime Profile、Model/Tool Catalog 和受控工具。 | 用户不能自行改变模型、提示策略、工具权限、Agent 图或运行时；新能力需经过平台维护者审核和部署。 | 使用已部署的受控 AGENT Task 能力；需要新 Agent 行为时提交版本化变更，由管理员评审、测试并发布固定 Catalog/Runtime。该决定不排除清单要求的现有 Agent Task 正向验收。 | 版本化 Agent 契约、工具权限、沙箱/资源界限、预算、可复现实验集、模型风险审查、回滚和供应链门禁均建立并通过后，重新评估受限 authoring。 |

## 影响与边界

- 任何以安全、隔离或信任边界为由改变现有产品承诺，都必须先形成并接受一项有记录的范围决定，写明受影响承诺、用户影响、风险依据、替代路径和复评条件；未经该决定不得从实现或验收范围中静默移除。若涉及风险接受，还须记录负责人和到期日。
- 以上决定只限定当前产品承诺，不把未实现能力写成已完成，也不降低 P8.3 性能、安全门禁、G4 灾备恢复或 G1 现有 Agent Task 验收要求。
- Join 延期不改变既有 Aggregate 及单输入算子的语义；跨主机 HA 排除不等于免做冷备份和灾难恢复。
- Bearer Token 决定不表示凭据生命周期已经完全收口；签发、分发、到期、撤销、轮换、丢失响应和 TLS 仍需通过 G8/G3 相应门禁。
- Marketplace 和 Agent authoring 的延期不能用于缩小现有受控算子、工作流、权限、预算或 Agent Task 承诺。

## 依据

- [V7 总体架构与首期边界](../architecture/v7-hybrid-architecture-plan.md)
- [V7 分阶段开发方案](../implementation-plan.md)
- [P7.3 Join / Aggregate 复评](../architecture/p73-join-aggregate-reassessment.md)
- [Capability Studio 实施范围](../architecture/capability-studio-implementation-plan.md)
- [P2 身份与角色契约](../contracts/p2-control-contracts.md)
- [P2 Token 运维说明](../operations/p2-database.md)
- [当前桌面入口与屏幕要求](../../README.md)
- [P8.2 单机部署与恢复](../operations/p8-unified-deployment.md)
