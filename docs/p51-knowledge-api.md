# P5.1 Knowledge API 与证据契约

P5.1 在 P4 执行平台之外增加可选的正文证据服务。Data 保存原始 Markdown/TXT 字节；Knowledge 通过认证 HTTP 读取精确 AssetRef，规范化、分段并建立词法索引。Knowledge 不读取 Data、Platform 或 Execution 的数据库。

## 文档资产

`POST /api/v1/projects/{project_id}/uploads` 的 `format` 增加 `markdown` 和 `text`。内容仍通过既有完整对象上传接口写入，finalize 请求为：

```json
{"kind":"DOCUMENT","profile":"utf8-text-v1"}
```

文件上限为 20 MiB，只接受 UTF-8。UTF-8 BOM 可识别并在 Knowledge 规范化正文中去除；无效 UTF-8、NUL、被禁控制字符、空白文件和二进制伪装明确失败。Data 的 `original` 表示保留上传字节及其摘要，不执行 Markdown，也不读取链接或 URL。

## Knowledge 路由

令 `P=/api/v1/projects/{project_id}`：

| 路由 | 角色 | 语义 |
| --- | --- | --- |
| `POST P/knowledge/imports` | EDITOR | 按精确 AssetRef 和 `original` 表示持久接受导入 |
| `GET P/knowledge/imports/{id}` | VIEWER | 查询排队、解析、索引或失败事实 |
| `GET P/knowledge/documents` | VIEWER | 分页发现当前项目版本 |
| `GET P/knowledge/documents/{document}/versions/{version}` | VIEWER | 查询精确版本与当前生命周期 |
| `POST P/knowledge/search` | VIEWER | 返回发现用 SearchHit，不产生 EvidenceRef |
| `GET .../versions/{version}/units/{unit}` | VIEWER | 精确读取，生成 EvidenceRef 和读取回执 |
| `POST .../versions/{version}/withdraw` | OWNER | 按预期生命周期修订撤回版本 |

状态写入要求 `Idempotency-Key`。Platform 在每次调用和重放前重新检查当前令牌及项目角色。Knowledge 的内部接口只接受服务令牌和 Platform 提供的操作者 ID。

## 正文与证据身份

`utf8-nfc-lf-v1` 将正文转换为 Unicode NFC，并把 CRLF/CR 统一为 LF。`paragraph-window-v1` 以标题和空行为逻辑边界；超过 2,000 个 Unicode 字符的段落按 2,000 字符窗口分割，相邻窗口重叠 200 字符。

EvidenceUnit 保存规范化正文上的 `[start_char,end_char)`、片段摘要、上下文及分段器版本。精确读取返回的 EvidenceRef 同时固定项目、文档、版本、单元、规范化正文摘要、字符范围和片段摘要。行号只适合显示，不承担身份。

搜索使用 PostgreSQL `simple` tsvector、`pg_trgm` 和确定性次序。搜索结果是候选，不含 EvidenceRef。默认搜索排除 `WITHDRAWN`；精确历史读取仍返回原文，同时报告当前生命周期。

生命周期的三个维度相互独立：

```text
availability: ACTIVE | WITHDRAWN
epistemic_status: DECLARED | CORROBORATED | CONFLICTING
review_state: UNREVIEWED | REVIEWED
```

P5.1 导入默认为 `ACTIVE + DECLARED + UNREVIEWED`。本轮不提供把资料自动升级为 CORROBORATED/REVIEWED 的接口。

## 错误边界

`NO_RESULTS` 是成功的空搜索。`RESOURCE_NOT_FOUND`、`SCOPE_DENIED`、`DEPENDENCY_UNAVAILABLE` 和撤回后的生命周期事实分别表达。Data 暂时不可达时，Knowledge Worker 把导入安全退回队列；它不会把通信故障伪造成永久内容失败。永久格式或绑定错误进入 `FAILED`。

