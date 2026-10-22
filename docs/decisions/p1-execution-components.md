# P1 执行组件选型记录

日期：2026-10-12。范围：组件最小可行性验证，正式集成属于 P4。

**后续诊断更正：**下表“本机直接 rootless gVisor 未通过”记录的是首次探针的实际结果；该探针混用了内置 `--rootless` 与 OCI user namespace 映射。修正后原生 rootless OCI 的启动、查询和取消通过，但需 `--ignore-cgroups`，仍未验证资源限额，也不替代已选的专用包装部署。见[最新证据](../verification/p1-rootless-correction-evidence.json)和[复验报告](../verification/p1-p3-runtime-followup-report.md)。Toil 选型不变。

## 决定

选择 **Toil 9.5.0** 作为后续 CWL 集成候选；通过当前 gVisor 专用包装容器的隔离配置。Weaver 6.16.0 保留对照实验与失败证据，不接入平台。

决策执行用户确认的规则：隔离先通过；两引擎都通过或仅 Toil 通过，选 Toil；仅 Weaver 通过，选 Weaver；都不通过则阻断。缺失、重复或 `NOT_RUN` 的检查不能凑成通过，未使用加权评分抵消失败。该规则由 [gate.py](../../experiments/p1/gate.py) 和单元测试执行。

验收依据是 [机器可读证据](../verification/p1-evidence.json)，不是文档宣称的能力。原始日志位置由其中 `run_directory` 和各组件 `log` 字段给出。

## 检查结果

| 组件与检查 | 结果 | 证据与含义 |
| --- | --- | --- |
| Toil 提交、查询、新客户端重定位、重复提交 | 通过 | 同一保存句柄定位原引擎，未新增执行 |
| Toil 嵌套重复值 scatter 身份 | 通过 | 两个相同值对应不同 job ID 和调用身份；静态路径映射与序号可核对 |
| Toil 故障后恢复 | 通过 | 首次工具失败，原 JobStore 恢复成功；同一工具 job ID、两个 attempt |
| Toil 取消 | 通过 | 真实工具已启动后取消，工具及引擎停止、无结果、重复取消返回终态 |
| Weaver 提交／查询／结果读取 | 通过 | REST 提交、查询、下载真实结果 14 |
| Weaver 嵌套 scatter 身份 | 通过 | 相同值有两个子 job ID、不同 ordinal，下载结果与提交参数可核对 |
| Weaver API 重启定位 | 通过 | API 重启后，原 job ID 可查询至成功 |
| Weaver 取消 | 未通过 | REST 状态为 dismissed 后，实际工具容器在 15 秒观察窗内仍运行 |
| gVisor 文件／网络／临时目录限制 | 通过 | 输入不可写、宿主文件不可见、只有 loopback、临时目录触发容量限制 |
| gVisor 定位与取消 | 通过 | 重新 inspect 原容器 ID 后取消真实 guest，无延迟输出 |
| 本机直接 rootless gVisor | 未通过，单独观测 | gofer 启动 `operation not permitted`；不属于通过的部署配置 |

## 必须保留的适配约束

1. **步骤身份不能只信 Toil 的简写名称。** 固定夹具的静态路径编码到 tool ID，再结合 scatter 序号和引擎 job ID。P4 必须扩展并验证通用路径、条件、多个层次 scatter 和重试身份；目前没有这种通用能力。
2. **定位失败不等于任务不存在。** 实验句柄存在但无法确认进程时保留 UNKNOWN，阻止自动重复提交。P2 需实现权威任务／行动账本，P4 再将引擎状态纳入对账；sidecar 不能替代这些组件。
3. **取消必须看实际计算。** Weaver 对照说明接口终态不能证明工具停止。后续控制核心必须区分取消意图、引擎响应与外部活动，并对迟到结果设置发布门槛。
4. **隔离有明确部署前提。** 通过的是有 SYS_ADMIN／SYS_PTRACE 的可信外层 Docker 容器托管无权限 gVisor guest。本机 rootless 路径不合格，不能将当前结果宣传为完全无特权部署或逃逸安全证明。
5. **试验与集成分开验收。** Toil 的受信任夹具目前在主机执行，gVisor 单独验收；二者尚未联调。平台没有生产 CWL 端口、调度 Worker 或远程执行实现。

## P4 仍需完成

CWL 顺序／并行／条件／子流程／多层 scatter 的完整支持与身份；QGIS 和制图环境；引擎与隔离运行时联调；远程未知状态、节点许可、旧 Worker、恢复／取消组合故障；正式输出、发布和科学验收；长作业资源限制及性能。P1 通过不能免除这些检查。

复现方法、固定版本与权限配置见 [实验说明](../../experiments/p1/README.md)。Weaver 当前失败只适用于本次固定镜像、配置和测试范围，不是对所有版本、部署或能力的否定。
