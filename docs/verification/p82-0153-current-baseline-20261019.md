# P8.2 / 0.15.3 当前发布基线审计

状态：`CURRENT BASELINE / RUNTIME DEGRADED`  
审计日期：2026-10-19（Asia/Shanghai）  
阶段 ID：`P8-CLOSEOUT-20261019-G0-G1`  
活动 Release：`0.15.3` / `sha256:ab0a7c60f17a81cb6a89668a9792e9807a10749ad70eaaade9a3ec2f13d6b3b1`

本报告是当前 0.15.3 发布身份和线上状态的唯一审计摘要。它保留 2026-10-18 完整回归结果，同时记录 2026-10-19 只读运行态核验。线上容器没有在本轮重启；没有读取或写入线上业务数据。

## 发布身份与制品

当前机器指针为 [p82-current-release.json](p82-current-release.json)。Release Lock schema v3 已通过 `load_release_lock` 校验，Lock digest 与指针一致。锁定目录清点结果如下：

| 项目 | 当前基线 | 核验结果 |
| --- | --- | --- |
| Release / 应用镜像 | `0.15.3` / `sha256:ab0a7c60…d6b3b1` / `sha256:7123ef9f…e3c4ba` | 指针、Lock、Gateway 响应头一致 |
| 源码树 | 460 个文件 / `sha256:3e2aed26…b5a043` | Lock 中登记；当前工作区源码已因未发行修复而不同 |
| Python wheel | 11 个 | 所有 wheel 文件摘要匹配 Lock |
| 依赖 wheel | 64 个 | 所有依赖文件摘要匹配 Lock |
| Web 资源 | 21 个，其中 JS 17 个 | 17 个 JS 均从 Gateway 读取并匹配 Lock；17 个 source map 均未公开 |
| 镜像引用 | 9 个 | app 镜像运行 ID 与 Lock 一致；Compose 的 PostgreSQL 配置引用 Lock 中固定摘要 |
| Operator / Checker / Tool Catalog | 11 / 11 / 23 项 | Catalog 文件摘要匹配 Lock |
| Model / Embedding Catalog | 1 个模型目录 / `DISABLED` | Model Catalog 文件摘要匹配 Lock；Embedding 未启用 |
| 数据库 schema | Platform `0023_workflow_release_lifecycle`; Data `0004_document_assets`; Execution `0006_remote_reliability`; Knowledge `0002_lifecycle_outbox` | 记录于 Lock |

发行目录摘要校验结果：11/11 wheel、64/64 依赖 wheel、4/4 Catalog 文件匹配；Lock 自身校验通过。前端资源嵌入应用 wheel 并由应用镜像提供，因此从线上 Gateway 对 17 个 JS 资源做摘要复核，结果 17/17 匹配；CSP 存在，source map 返回 404。结构化详情见 `.p8-runtime/verification/live-audit-20261019/release-assets.json`。

本轮又对本地不可变 Release 目录进行了独立只读重算：Lock canonical digest 与指针一致；11/11 wheel、64/64 依赖 wheel、21/21 Web 资源（从应用 wheel 内逐项读取）、Operator/Checker/Tool/Model/Runtime 文件摘要及内部摘要均匹配；Embedding Catalog 为 `DISABLED`，其摘要与 Runtime Profile 绑定一致。机器证据见 `.p8-runtime/verification/p8-closeout-20261019-G0-G1/active-0153-release-integrity-audit.json`。但当前源码清单重算为 471 文件 / `sha256:f7b3729f…ae4839`，与活动 Lock 的 460 文件 / `sha256:3e2aed26…b5a043` 不同；活动 Release 目录只有 11 个包级 sdist，没有整仓源码清单或源码快照；这些 sdist 不能重建 460 文件的整仓来源。本地应用镜像的 OCI revision label 为 `sha256:13228c97…21dde6`，与 Lock 的源码摘要不同；构建脚本以发行目录为 Docker 构建上下文；Dockerfile 只复制其中的 wheel 和依赖，不复制整仓源码快照。用户确认目前没有原始归档或 commit，因此不能复现该摘要，也不能关闭 G0 对此项的要求。

源码树摘要是活动 Release 锁定时的源码身份。当前工作区在该 Release 之后已修改 Worker 心跳逻辑、当前报告门禁和对应测试；这部分修改尚未打包、签名或部署，不能归入 `0.15.3`。

## 回归基线

2026-10-18 的权威全量回归为 **1106 passed / 0 failed / 0 errors / 0 skipped**，详见[结构化摘要](../../.p8-runtime/verification/full-current-20261018/summary.json)。先前历史修复报告中的 `1101`、`1104` 是中间或旧轮次数字，不代表当前全量结果。

这次工作区修复的聚焦验证为 Ruff 通过、6 项 pytest 通过，命令：

```bash
uv run --locked ruff check packages/platform-infrastructure/src/platform_infrastructure/worker.py scripts/build_current_full_report.py tests/p2/test_worker_loop.py tests/p82/test_current_report.py
uv run --locked pytest -q tests/p2/test_worker_loop.py tests/contracts/test_health.py tests/p82/test_current_report.py
```

聚焦验证只证明当前源码中的心跳与报告门禁修复，不替代全量兼容回归或发行包验证。

## 线上运行态只读观察

观察时间：`2026-10-18T20:29:19Z`（2026-10-19 04:29 Asia/Shanghai）。Gateway loopback `http://127.0.0.1:18080` 与受信 LAN `http://192.168.167.87:18080` 的 `/api/v1/health`、`/api/v1/ready`、`/gateway/health` 均返回 HTTP 200，报告的 Release digest 均与当前指针一致。根页面带 CSP；JS 资源摘要一致；source map 返回 404。

Compose 项目 `groundwater-v7-p8-production-final` 有 17 个运行容器，16 个 healthy、1 个 unhealthy；所有运行容器重启计数均为 0，最近 10 分钟错误日志扫描命中 0 行。唯一异常为 `platform-worker`：进程仍运行，但 health check 因心跳文件过期标为 unhealthy。当前实现只在 Worker 启动时写心跳，空闲轮询不续写；这属于健康探针实现缺陷，不是已确认的业务队列故障。生产业务队列和数据库未被本次探测读取。

工作区修复包含 Worker 空闲循环续写心跳及 `workspace-summary` 游标处理。它们已进入隔离的 `0.15.5` 候选；候选栈快照为 17/17 服务健康，浏览器 101 项验收中的 `workspace-summary` 返回 200。详见[0.15.5 候选验收报告](p8-closeout-0155-candidate-report.md)。候选全量回归没有权威结果，且尚未部署；生产指针仍指向 `0.15.3`，因此线上 `platform-worker` 异常仍未修复，生产状态保持 `DEGRADED`。

完整只读探测数据见 `.p8-runtime/verification/live-audit-20261019.json`。本审计是单一时点快照，不构成持续可用性证明。

## 当前门禁判断

- G0 仍为 `PARTIAL`：Release Lock 和已打包制品身份已复核；静态链接、脚本路径和命令接口审计已通过。活动 Release 的原始源码树仍无法复现，未完成范围决策及正式 PASS 证据仍未完成。
- 隔离完整 Compose 栈上的 90 个认证 OpenAPI operation 已重验，0 个认证失败、方法错误、内部错误或 503；原证据中的 11 个依赖路由另以合成 Asset/Document 取得 200 正向响应。详见[认证 OpenAPI 结构化证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/authenticated-openapi-live.json)和[原 11 个依赖路由正向证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/former-503-positive-routes.json)。这关闭 `OPENAPI_DEPENDENCY_UNAVAILABLE` 的证据缺口，但并不等于 90 个 operation 全部取得成功响应。
- Upload、Asset、Knowledge 三类目录各 101 项的 API 游标分页均通过；真实 Gateway 浏览器验证了资产第 101 项、上传对话框流程和刷新后令牌清空，见[101 项结构化证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/browser-101-live.json)与[浏览器上传证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/browser-upload-flow.json)。
- G1 仍为 `PARTIAL`：活动 `0.15.3` 的 101 项浏览器项目概览曾触发 `workspace-summary` HTTP 400 `MALFORMED_REQUEST`，旧版复现见[失败证据](../../.p8-runtime/verification/p8-closeout-20261019-G0-G1/workspace-summary-101-failure.json)。隔离候选 `0.15.5` 的 Gateway 浏览器复验已返回 200，但候选没有权威全量回归且尚未部署；Agent、取消、Knowledge 撤回、90 个 operation 的合法成功响应/schema 等仍未完成。
- 生产运行态为 `DEGRADED`：`platform-worker` health check 失败，不能把该运行态当作 G1 全服务通过证据。
- 原全量摘要中的两个 Medium limitation 已分别取得隔离完整栈证据；摘要本身仍是 2026-10-18 的历史全量回归，不在本次修改，等完整 G1 浏览器闭环与最终回归后再生成当前摘要。

下一步构建新版本候选以包含 Worker 心跳和 workspace-summary 分页修复，再在带唯一测试标签的隔离 Compose 栈复跑浏览器端到端流；不能连接生产数据库或复用生产凭据。
