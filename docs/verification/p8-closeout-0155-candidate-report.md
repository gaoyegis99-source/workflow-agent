# P8 收口候选 0.15.5 验收报告

> **CANDIDATE / NOT ACTIVE.** 本报告记录 0.15.5 隔离验收结果；当前生产 Release 仍以同目录的 [当前发布指针](p82-current-release.json) 为准。

日期：2026-10-19  
阶段：`P8-CLOSEOUT-20261019-G0-G1`  
结论：**候选关键链路通过；G0/G1 保持 PARTIAL；未部署**

## 发布身份

- 候选版本：`0.15.5`
- Release digest：`sha256:acd8c2b48041f70b8b4a18cb822c9f7d579a9fe2112166892387283df6b3bd61`
- 候选目录：`.p8-runtime/releases/0.15.5-acd8c2b48041f70b8b4a18cb822c9f7d579a9fe2112166892387283df6b3bd61`
- 源码摘要：`sha256:f7b3729f4a0b515910aa1d1d2cdbcff82f1f2f14b1d1359b808a7f8745ae4839`（471 个文件）
- 应用镜像标签 revision 与 Release Lock 的源码摘要一致；90 个 OpenAPI 探针响应中的 Release digest 也与候选锁一致。
- 当前生产指针仍为 `0.15.3` / `sha256:ab0a7c60f17a81cb6a89668a9792e9807a10749ad70eaaade9a3ec2f13d6b3b1`；本报告没有执行部署或指针切换。

构建核对发现此前 Lock 与应用镜像标签使用不同源码文件清单。`scripts/build_p7_release_lock.py` 已改为共用 `scripts/release_source_manifest.py` 的清单生成口径。该修复只进入未来候选；活动 0.15.3 保留其原始身份，不会被本次报告改写。0.15.4 是不可变旧候选，因源码摘要不一致而不作为权威证据；它没有被覆盖，失效原因见[结构化记录](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0154/attempt-status.json)。

## 隔离运行栈

验收实例：`p8-test-g1b-20261019`，Gateway 只绑定 `127.0.0.1:28380`。结构化运行快照显示 17/17 个运行服务 healthy、重启数为 0；19 个容器（包含两个成功退出的一次性初始化容器）和测试网络均带唯一 `v7.test-run` 标签。Platform、Data、Knowledge API 没有宿主端口绑定。所有应用容器使用候选镜像和源码摘要。

运行证据：[Compose 栈快照](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/compose-stack-runtime.json)。候选栈在验收后已停止，没有删除其实例数据目录。

## 通过的候选验收

- 90 个公开 OpenAPI operation 均经过 Gateway 认证路由，无 401、403、405、500 或 503；状态为 200:18、201:2、400:35、404:35。它证明路由可达，不证明 90 个 operation 的成功请求和响应 schema 全覆盖。[OpenAPI 证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/authenticated-openapi-live.json)
- Upload、Asset、Knowledge 三类目录各 101 项；Chromium 经 Gateway 展示第 101 个资产。上传完成到 `READY`，`workspace-summary` 返回 200，精确资产与 Knowledge 版本/EvidenceUnit 查询成功；刷新后 Token 清空，页面错误、API 错误和控制台错误为 0。[浏览器与分页证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/browser-101-live.json)
- Direct Task 执行两次并通过独立验收；任务到 `COMPLETED`，两项成果下载均为 200，预算使用 3、预留和执行中余额均为 0，测试 Token 在清理阶段撤销。[Direct 证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/direct-acceptance-live.json)
- Workflow Validation、Candidate、Trial 和 Release 执行通过；三步均成功，工作流释放版本为 `1.0.0`、资格为 `SAMPLE_VERIFIED`，停用/重新激活后的活动修订为 3。StaticMap PNG 下载 200、75,308 字节；浏览器页面/API 错误为 0，最终预算使用 6、预留及执行中余额为 0。[空间工作流证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/spatial-workflow-live.json)
- OWNER/EDITOR 可写，VIEWER 写入被 403 拒绝；跨项目读取为 404；短时 Token 到期前 200、过期后 401，临时 Token 全部撤销。[权限与到期证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/rbac-expiry-live.json)
- Data API 停止时 Gateway 返回 `DEPENDENCY_UNAVAILABLE`/503；服务恢复后浏览器重连回到 200，无页面错误。[依赖恢复证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/candidate-0155/dependency-recovery-live.json)

## 未通过或尚未证明

- G1 仍为 `PARTIAL`：没有完成 AGENT Task 的真实正向闭环；Agent Model Catalog 当前为禁用配置，本轮没有调用外部模型。
- 90 个 operation 仍缺逐项合法成功请求、正向资源状态和响应 schema 覆盖；35 个 400 与 35 个 404 仅证明请求到达路由后的预期拒绝分支。
- 浏览器完整闭环仍缺取消、刷新后精确版本定位、Knowledge 撤回，以及 CSP、source map、敏感信息和横向溢出检查。
- 0.15.5 的完整兼容回归尚无权威结果。两次单独尝试均因同一宿主上出现独立 V6 pytest 作业而中断，记录为无效尝试，不用于发布背书：[第一次尝试](../../.p8-runtime/verification/full-current-20261019-0155/attempt-status.json)、[第二次尝试](../../.p8-runtime/verification/full-current-20261019-0155-rerun-02/attempt-status.json)。历史 0.15.3 的 1106/1106 结果不能替代候选回归。
- P8.3（性能/容量/组合故障）和 P8.4（安全、SBOM、签名、离线发行）仍未开始。灾备目标、科学可信度、生产文档检索、Join 范围及可观测性也未通过各自门禁。

当前门禁状态：G0 `PARTIAL`、G1 `PARTIAL`、G2 `NOT_STARTED`、G3 `NOT_STARTED`、G4 `PARTIAL`、G5 `PARTIAL`、G6 `PARTIAL`、G7 `DECISION_REQUIRED`、G8 `PARTIAL`、G9 `NOT_STARTED`。按清单“只有 PASS 计分”的规则，当前收口门禁得分为 `0/100`；这不是功能完成率，不能替代产品范围分析。

因此，本候选不能提升为当前 Release，也不能据此宣布 V7 100% 完成。
