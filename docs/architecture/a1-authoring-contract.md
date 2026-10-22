# A1 / 0.14.0 工作流草稿与算子描述符契约

A1 在现有 Operator Catalog 与执行面之前增加只负责编辑的控制面。草稿不会创建 Action，
不预留预算，也不会形成可执行 WorkflowPackage。执行仍只能使用既有冻结包。

## 权威对象

`WorkflowDefinition` 保存项目、稳定键、当前头修订和创建事实。稳定键及定义身份创建后不
可变。标题、说明、标签和布局属于 `WorkflowRevision`；每次保存都追加不可变修订。

revision 1 只能由服务端模板 `blank-workflow-v1` 生成：

```json
{
  "cwlVersion": "v1.2",
  "class": "Workflow",
  "inputs": {},
  "outputs": {},
  "steps": {}
}
```

A1 的追加请求只接受 `expected_revision`、`source_digest`、`metadata` 和 `layout`。服务端从
前一修订复制 source；请求携带 source、steps 或实现引用会在 DTO 层拒绝。并发保存先锁定
definition 行，只有一个请求能够从同一头修订前进。

## 摘要边界

- `source_digest` 只覆盖规范 JSON source。
- `layout_digest` 只覆盖编辑器布局。
- `semantic_contract_digest` 覆盖算子身份、输入、输出、参数 Schema 及服务端校验要求。
- `presentation_digest` 只覆盖可信 UI hints。
- `descriptor_digest` 覆盖两类信息及其摘要。

布局变化不会改变 source 身份；展示提示变化不会改变科学语义身份。

## 算子描述符

描述符从已校验的 Operator Catalog 和现有 Pydantic 参数模型确定生成。当前精确覆盖 11 个
`COMPUTE_OPERATOR`。系统工具和 checker 没有参数编辑入口，也不会出现在返回结果中。

JSON Schema 用于客户端表单和基本约束；`server_validation_required=true` 表示仍需执行
Pydantic 的跨字段或集合校验。例如 `ROW_COUNT` 边界、`COUNT_ROWS` 字段形状和字段去重
不能由客户端 Schema 判断结果替代服务端校验。

## 接口

令 `P=/api/v1/projects/{project_id}`：

| 方法 | 路径 | 权限 | 行为 |
|---|---|---|---|
| GET | `P/authoring/operator-descriptors` | VIEWER | 返回与部署目录摘要绑定的描述符 |
| POST | `P/workflow-definitions` | EDITOR | 原子创建定义和 revision 1 |
| GET | `P/workflow-definitions` | VIEWER | 按定义 ID 分页 |
| GET | `P/workflow-definitions/{id}` | VIEWER | 返回定义和当前头修订 |
| POST | `P/workflow-definitions/{id}/revisions` | EDITOR | 追加元数据/布局修订 |
| GET | `P/workflow-definitions/{id}/revisions` | VIEWER | 按 revision 分页 |
| GET | `P/workflow-definitions/{id}/revisions/{revision}` | VIEWER | 精确历史读取 |

所有 POST 要求 `Idempotency-Key`。创建与追加使用不同 operation identity；同键同内容返回原
结果，同键不同内容返回 `409 IDEMPOTENCY_CONFLICT`。重放前重新检查当前令牌和项目成员关系。

## 明确边界

A1 没有节点画布、source 修改、候选快照、验证 Worker、试运行、发布、提案、算子注册和删除
接口。`/studio/` 只提供算子查看、空白定义创建和元数据修订。
