# Capability Studio A4 / 0.14.3 算子接入材料契约

## 定位

A4 管理“接入材料、评审证据、部署匹配”三类事实。它不安装插件，不构建镜像，也不把项目 OWNER 提升为平台维护者。计算能力仍只来自部署时固定的 Operator Catalog、镜像摘要和 Release Lock。

公开流程为：

```text
EDITOR/OWNER 创建材料草稿
  → 追加不可变材料修订
  → 提交精确修订
  → 维护者在受控 CLI 导入评审决定
  → 维护者在实际发行后导入部署证据
  → API 用当前 Catalog/Release Lock 实时计算 deployment_match
```

`APPROVED` 只表示材料修订通过评审。只有部署回执、当前 Release Lock、Operator Catalog、算子语义、实现、镜像、源码和测试身份全部一致，且目录生命周期为 `ACTIVE`，才返回 `MATCHED_ACTIVE + current_executable=true`。

## 材料边界

材料固定包含：

- 算子 ID、版本、作用、限制、输入/参数/输出契约及语义摘要；
- 本地受信实现 ID、固定镜像、运行时、已知协议和资源档及实现摘要；
- 包名、包版本、来源说明、源码摘要、测试摘要和 Checker 覆盖说明。

协议和资源档只能从现有白名单选择。材料 JSON 最大 128 KiB、嵌套不超过 16 层、最多 4096 个值。`package_source` 是纯来源文本；服务不会把它当 URL 获取，也不会运行其中的命令。客户端不能提交生命周期、容器命令、任意 CPU/内存值、宿主路径或远程绑定。

算子和实现摘要按规范 JSON 重新计算。现有 Operator Catalog 中同一算子版本若语义摘要不同，提交时返回 `REVISION_CONFLICT`，必须使用新版本表达语义变化。

## 持久事实

Platform `0022_operator_proposals` 新增：

- `operator_proposals`：项目内稳定系列及 head；
- `operator_proposal_revisions`：不可变材料和摘要；
- `operator_review_events`：追加式提交/评审历史；
- `operator_deployment_receipts`：维护者导入的实际发行证据。

状态从 head revision 的事件投影：`DRAFT`、`SUBMITTED`、`CHANGES_REQUESTED`、`APPROVED`、`REJECTED`。修改材料创建新 revision，旧事件和旧回执继续可读。数据库不保存会随部署漂移的 `enabled` 字段。

部署匹配状态为：

- `NOT_APPROVED`、`NO_RECEIPT`；
- `RELEASE_MISMATCH`、`CATALOG_MISMATCH`；
- `OPERATOR_NOT_FOUND`、`IMPLEMENTATION_NOT_FOUND`、`MATERIAL_MISMATCH`；
- `MATCHED_ACTIVE`、`MATCHED_DISABLED`、`MATCHED_REVOKED`。

`DISABLED` 和 `REVOKED` 均返回不可执行，历史材料、评审和成果不删除。

## 接口与权限

项目接口：

- `POST/GET /api/v1/projects/{project_id}/operator-proposals`
- `GET /api/v1/projects/{project_id}/operator-proposals/{proposal_id}`
- `POST /api/v1/projects/{project_id}/operator-proposals/{proposal_id}/revisions`
- `POST /api/v1/projects/{project_id}/operator-proposals/{proposal_id}/submit`

VIEWER 只读；EDITOR/OWNER 可创建、修订和提交。所有写入使用 `Idempotency-Key` 并在重放时重新校验成员权限。公开 OpenAPI 不包含审核、部署或目录变更路由。

维护者使用：

```bash
platform-admin operator-proposal-review ...
platform-admin operator-proposal-deployment-import ...
```

两条命令都要求 `V7_OPERATOR_CATALOG`、`V7_RELEASE_LOCK` 和 `V7_RELEASE_DIGEST`，并核对实际 Catalog 文件和 Release Lock。维护者身份不会作为项目 Token 下发浏览器。

## 明确限制

A4 没有通用插件 ABI、代码上传、URL 抓取、Git clone、pip install、镜像构建、任意命令执行、项目自审批、网页部署和算子市场。新增算法仍需受控代码开发、独立测试、目录生成、镜像构建及完整发行流程。A4 只让这些材料和最终运行事实可以精确对应和复核。

